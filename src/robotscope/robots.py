"""robots.txt parsing, versioned caching and admission evidence.

Design rules
------------
* Admission is decided per ``(host, path, user-agent)``.
* Every decision carries a version identifying the exact ruleset that produced
  it (or the exact failure that prevented one).
* A missing cache entry, an expired entry that could not be refreshed, and a
  failed fetch are *three different outcomes*.  None of them is treated as
  "allow by default".
* A 404/410 response is positive evidence that the host publishes no rules and
  therefore allows crawling; the evidence is still versioned.
* Policy/site priority is never consulted here: robots denial is absolute.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import urls as urlmod
from .clock import resolve_now

DEFAULT_TTL = 3600.0
DEFAULT_BACKOFF = 300.0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    field: str          # "allow" | "disallow"
    pattern: str        # raw path pattern
    line: int

    @property
    def allow(self) -> bool:
        return self.field == "allow"


@dataclass(frozen=True)
class RuleMatch:
    allowed: bool
    reason: str
    pattern: Optional[str] = None
    field: Optional[str] = None
    line: Optional[int] = None
    group: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "pattern": self.pattern,
            "field": self.field,
            "line": self.line,
            "group": self.group,
        }


@dataclass
class RobotsRules:
    """A parsed robots.txt document.

    ``groups`` maps a lower-cased user-agent token to its rules.  ``"*"`` is
    the fallback group.  ``crawl_delay`` is kept per group.
    """

    raw: str = ""
    groups: dict[str, list[Rule]] = field(default_factory=dict)
    crawl_delay: dict[str, Optional[float]] = field(default_factory=dict)
    sitemaps: list[str] = field(default_factory=list)
    digest: str = ""

    # -- parsing -------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        raw = text or ""
        rules = cls(raw=raw, digest=hashlib.sha256(raw.encode("utf-8")).hexdigest())
        current_agents: list[str] = []
        in_rules = False
        for lineno, line in enumerate(raw.splitlines(), start=1):
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, value = line.split(":", 1)
            field_name = field_name.strip().lower()
            value = value.strip()
            if field_name == "user-agent":
                if in_rules:
                    current_agents = []
                    in_rules = False
                current_agents.append(value.lower())
                if not value:
                    current_agents.pop()
                continue
            if field_name == "sitemap":
                if value:
                    rules.sitemaps.append(value)
                continue
            if not current_agents:
                continue
            in_rules = True
            if field_name in ("allow", "disallow"):
                if not value:
                    # ``Disallow:`` with an empty value imposes no restriction;
                    # ``Allow:`` with an empty value is equally a no-op.
                    continue
                for agent in current_agents:
                    rules.groups.setdefault(agent, []).append(Rule(field_name, value, lineno))
            elif field_name == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                for agent in current_agents:
                    rules.crawl_delay[agent] = max(0.0, delay)
        return rules

    # -- user-agent group selection -----------------------------------
    def matching_group(self, user_agent: str) -> Optional[str]:
        """"Most specific matching group wins", per RFC 9309.

        Matching compares the agent *token* against the user-agent product
        token, case-insensitively.  ``*`` is the fallback.  Groups that only
        declare ``Crawl-delay`` (no rules) still count as present.
        """
        token = (user_agent or "").strip().lower()
        agents = set(self.groups) | set(self.crawl_delay)
        best: Optional[str] = None
        best_len = -1
        for agent in agents:
            if agent == "*":
                if best is None:
                    best = "*"
                continue
            if agent and agent in token and len(agent) > best_len:
                best = agent
                best_len = len(agent)
        if best is None and "*" in agents:
            best = "*"
        return best

    def delay_for(self, user_agent: str) -> Optional[float]:
        group = self.matching_group(user_agent)
        if group is None:
            return None
        if group in self.crawl_delay:
            return self.crawl_delay[group]
        return self.crawl_delay.get("*")

    # -- decision ------------------------------------------------------
    def decide(self, path: str, user_agent: str) -> RuleMatch:
        group = self.matching_group(user_agent)
        if group is None:
            return RuleMatch(True, "no_matching_group")
        # Longest matching pattern wins; allow wins ties (RFC 9309).
        best: Optional[Rule] = None
        best_len = -1
        for rule in self.groups.get(group, []):
            if not _pattern_matches(rule.pattern, path):
                continue
            length = len(rule.pattern)
            if length > best_len or (length == best_len and rule.allow and best is not None and not best.allow):
                best = rule
                best_len = length
        if best is None:
            return RuleMatch(True, "no_matching_rule", group=group)
        return RuleMatch(
            best.allow,
            "allowed" if best.allow else "disallowed",
            pattern=best.pattern,
            field=best.field,
            line=best.line,
            group=group,
        )


def _pattern_matches(pattern: str, path: str) -> bool:
    if pattern == "":
        return True
    anchored_end = pattern.endswith("$")
    if anchored_end:
        pattern = pattern[:-1]
    regex = ""
    for ch in pattern:
        if ch == "*":
            regex += ".*"
        else:
            regex += re.escape(ch)
    if anchored_end:
        return re.fullmatch(regex, path) is not None
    return re.match(regex, path) is not None


def parse_robots(text: str) -> RobotsRules:
    return RobotsRules.parse(text)


# ---------------------------------------------------------------------------
# Fetch abstraction
# ---------------------------------------------------------------------------
@dataclass
class FetchResponse:
    """Result of fetching a robots.txt file.

    ``fetcher`` callables may return this object, a ``(status, text, headers)``
    tuple, or a plain string (interpreted as HTTP 200).  Exceptions are treated
    as fetch failures.
    """

    status: int = 200
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    @classmethod
    def coerce(cls, value: Any) -> "FetchResponse":
        if isinstance(value, FetchResponse):
            return value
        if isinstance(value, tuple):
            status = int(value[0])
            text = value[1] if len(value) > 1 and value[1] is not None else ""
            headers = value[2] if len(value) > 2 and value[2] else {}
            return cls(status=status, text=str(text), headers=dict(headers))
        if isinstance(value, str):
            return cls(200, value)
        if value is None:
            return cls(200, "")
        raise TypeError(f"unsupported fetch result: {type(value)!r}")


# ---------------------------------------------------------------------------
# Versioned cache
# ---------------------------------------------------------------------------
@dataclass
class Evidence:
    """Why a particular URL was allowed or denied."""

    host: str
    path: str
    user_agent: str
    allowed: bool
    reason: str
    version: Optional[str]
    status: str
    matched: Optional[RuleMatch] = None
    fetched_at: Optional[float] = None
    expires_at: Optional[float] = None
    http_status: Optional[int] = None
    digest: Optional[str] = None
    stale: bool = False
    backoff_until: Optional[float] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "path": self.path,
            "user_agent": self.user_agent,
            "allowed": self.allowed,
            "reason": self.reason,
            "version": self.version,
            "status": self.status,
            "rule": self.matched.as_dict() if self.matched else None,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "http_status": self.http_status,
            "digest": self.digest,
            "stale": self.stale,
            "backoff_until": self.backoff_until,
            "error": self.error,
        }


def _version_for(status: str, text: str) -> str:
    digest = hashlib.sha256((status + "\n" + (text or "")).encode("utf-8")).hexdigest()
    return f"r1:{digest[:16]}"


def _ttl_from_headers(headers: dict[str, str], default: float) -> float:
    value = (headers or {}).get("cache-control") or (headers or {}).get("Cache-Control") or ""
    match = re.search(r"max-age\s*=\s*(\d+)", value, re.IGNORECASE)
    if match:
        return max(1.0, float(match.group(1)))
    return default


class RobotsStore:
    """Persist fetched robots files and answer admission questions.

    The store never silently falls back to "allow".  Callers that cannot fetch
    (for example a dry planning pass) use :meth:`check` with
    ``refresh=False`` and receive an explicit ``cache_missing``/expired denial
    instead of permission.
    """

    def __init__(self, state, *, ttl: float = DEFAULT_TTL, backoff: float = DEFAULT_BACKOFF) -> None:
        self.state = state
        self.ttl = float(ttl)
        self.backoff = float(backoff)

    # -- cache primitives ---------------------------------------------
    def current(self, host: str, user_agent: str):
        return self.state.one(
            "SELECT c.* FROM robots_current cur JOIN robots_cache c "
            "ON c.host=cur.host AND c.user_agent=cur.user_agent AND c.version=cur.version "
            "WHERE cur.host=? AND cur.user_agent=?",
            (host, user_agent),
        )

    def meta(self, host: str):
        return self.state.one("SELECT * FROM robots_meta WHERE host=?", (host,))

    def store(self, host: str, user_agent: str, *, status: str, text: str, http_status: Optional[int],
              now: float, ttl: Optional[float] = None, headers: Optional[dict] = None,
              error: Optional[str] = None) -> str:
        version = _version_for(status, text if status in ("present", "absent") else "")
        ttl = self.ttl if ttl is None else ttl
        expires = now + ttl if status in ("present", "absent") else now
        digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest() if status == "present" else None
        with self.state.transaction():
            self.state.execute(
                "INSERT INTO robots_cache(host,user_agent,version,digest,status,fetched_at,expires_at,"
                "backoff_until,http_status,error,content,rules,created_seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(host,user_agent,version) DO UPDATE SET fetched_at=excluded.fetched_at,"
                "expires_at=excluded.expires_at,backoff_until=excluded.backoff_until,"
                "http_status=excluded.http_status,error=excluded.error,content=excluded.content,"
                "rules=excluded.rules",
                (host, user_agent, version, digest, status, now, expires, None, http_status, error,
                 text or "", None, 0),
            )
            self.state.execute(
                "INSERT INTO robots_current(host,user_agent,version) VALUES(?,?,?) "
                "ON CONFLICT(host,user_agent) DO UPDATE SET version=excluded.version",
                (host, user_agent, version),
            )
            self.state.execute(
                "INSERT INTO robots_meta(host,content,digest,etag,last_modified,fetched_at,expires_at,"
                "http_status,status,backoff_until,error) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(host) DO UPDATE SET content=excluded.content,digest=excluded.digest,"
                "etag=excluded.etag,last_modified=excluded.last_modified,fetched_at=excluded.fetched_at,"
                "expires_at=excluded.expires_at,http_status=excluded.http_status,status=excluded.status,"
                "backoff_until=excluded.backoff_until,error=excluded.error",
                (host, text or "", digest, (headers or {}).get("etag"), (headers or {}).get("last-modified"),
                 now, expires, http_status, status, None if status in ("present", "absent") else now + self.backoff,
                 error),
            )
        self.state.event("robots_fetched", detail={
            "host": host, "user_agent": user_agent, "version": version, "status": status,
            "http_status": http_status, "error": error, "ttl": ttl,
        }, at=now)
        return version

    def _record_failure(self, host: str, user_agent: str, *, now: float, error: str,
                        http_status: Optional[int], backoff_until: float) -> str:
        with self.state.transaction():
            self.state.execute(
                "INSERT INTO robots_meta(host,content,digest,etag,last_modified,fetched_at,expires_at,"
                "http_status,status,backoff_until,error) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(host) DO UPDATE SET fetched_at=excluded.fetched_at,expires_at=excluded.expires_at,"
                "http_status=excluded.http_status,status=excluded.status,backoff_until=excluded.backoff_until,"
                "error=excluded.error",
                (host, "", None, None, None, now, now, http_status, "fetch_error", backoff_until, error),
            )
        self.state.event("robots_fetch_failed", detail={
            "host": host, "user_agent": user_agent, "error": error, "http_status": http_status,
            "backoff_until": backoff_until,
        }, at=now)
        return f"err:{hashlib.sha256((host + error).encode()).hexdigest()[:12]}"

    # -- refresh + decision -------------------------------------------
    def refresh(self, host: str, user_agent: str, fetcher: Callable[..., Any], *,
                now: Optional[float] = None, force: bool = False,
                scheme: str = "https") -> Optional[str]:
        """Fetch and cache ``/robots.txt`` for *host*.

        Returns the version of the evidence, or ``None`` when the cache is
        still fresh and no fetch was needed.  Failures are recorded with a
        backoff and **do not** invalidate the previously cached version.
        ``scheme`` selects the origin used for the fetch (defaults to https);
        cache entries are per host and user-agent as required.
        """
        now = resolve_now(self.state.clock, now)
        row = self.current(host, user_agent)
        meta = self.meta(host)
        if row is not None and not force and row["expires_at"] is not None and now < row["expires_at"]:
            return None
        if meta is not None and meta["backoff_until"] is not None and now < meta["backoff_until"] and not force:
            return None
        scheme = "http" if str(scheme).lower() == "http" else "https"
        robot_url = f"{scheme}://{host}/robots.txt"
        headers = {}
        if row is not None:
            if row["digest"]:
                headers["if-none-match"] = f'"{row["digest"][:16]}"'
        try:
            raw = fetcher(robot_url)
            response = FetchResponse.coerce(raw)
        except Exception as exc:  # network error, timeout, DNS, ...
            return self._record_failure(
                host, user_agent, now=now, error=f"{type(exc).__name__}: {exc}",
                http_status=None, backoff_until=now + self.backoff,
            )
        if response.error:
            return self._record_failure(
                host, user_agent, now=now, error=str(response.error), http_status=response.status,
                backoff_until=now + self.backoff,
            )
        status_code = int(response.status or 0)
        if status_code == 304 and row is not None and row["status"] in ("present", "absent"):
            # Revalidation succeeded; extend the lease on the same version.
            with self.state.transaction():
                self.state.execute(
                    "UPDATE robots_cache SET fetched_at=?,expires_at=? WHERE host=? AND user_agent=? AND version=?",
                    (now, now + self.ttl, host, user_agent, row["version"]),
                )
                self.state.execute(
                    "UPDATE robots_meta SET fetched_at=?,expires_at=?,status=?,backoff_until=NULL,error=NULL "
                    "WHERE host=?", (now, now + self.ttl, row["status"], host),
                )
            self.state.event("robots_revalidated", detail={
                "host": host, "version": row["version"], "status": row["status"],
            }, at=now)
            return row["version"]
        if status_code in (404, 410):
            return self.store(host, user_agent, status="absent", text="", http_status=status_code, now=now)
        if status_code == 200 or (200 <= status_code < 300):
            ttl = _ttl_from_headers(response.headers, self.ttl)
            return self.store(host, user_agent, status="present", text=response.text,
                              http_status=status_code, now=now, headers=response.headers, ttl=ttl)
        # Any other status is not permission to crawl.
        return self._record_failure(
            host, user_agent, now=now, error=f"HTTP {status_code}", http_status=status_code,
            backoff_until=now + self.backoff,
        )

    def check(self, url: str, user_agent: str, *, now: Optional[float] = None,
              refresh: bool = False, fetcher: Optional[Callable[..., Any]] = None,
              force: bool = False) -> Evidence:
        """Answer whether *url* may be fetched.

        ``refresh=True`` requires a *fetcher*.  ``refresh=False`` never fetches
        and will deny with ``cache_missing``/``cache_expired`` rather than
        assuming permission.

        The queried URL always uses ``https`` for robots lookup.  Callers that
        know the origin scheme can pass ``force`` to re-fetch; mixed-scheme
        hosts should use one :class:`RobotsStore` per scheme if they differ.
        """
        now = resolve_now(self.state.clock, now)
        host = urlmod.host_of(url)
        path = urlmod.path_of(url)
        if refresh:
            if fetcher is None:
                raise ValueError("refresh=True requires a fetcher")
            scheme = urlmod.split(url).scheme or "https"
            self.refresh(host, user_agent, fetcher, now=now, force=force, scheme=scheme)
        row = self.current(host, user_agent)
        meta = self.meta(host)
        if row is None:
            reason = "cache_missing"
            if meta is not None and meta["status"] == "fetch_error":
                reason = "fetch_failed"
            return Evidence(
                host=host, path=path, user_agent=user_agent, allowed=False, reason=reason,
                version=None, status="missing", error=(meta["error"] if meta is not None else None),
                backoff_until=(meta["backoff_until"] if meta is not None else None),
            )
        expired = row["expires_at"] is not None and now >= row["expires_at"]
        failure = meta is not None and meta["status"] == "fetch_error"
        if row["status"] == "absent":
            if expired:
                # An expired "no rules" answer is not permission to crawl.
                return self._stale_evidence(row, path, user_agent, expired=True, failure=failure,
                                            reason="stale_unverified")
            return Evidence(host=host, path=path, user_agent=user_agent, allowed=True,
                            reason="robots_absent", version=row["version"], status="absent",
                            fetched_at=row["fetched_at"], expires_at=row["expires_at"],
                            http_status=row["http_status"])
        if row["status"] != "present":
            return Evidence(host=host, path=path, user_agent=user_agent, allowed=False,
                            reason="fetch_failed", version=row["version"], status=row["status"],
                            fetched_at=row["fetched_at"], expires_at=row["expires_at"],
                            http_status=row["http_status"], error=row["error"],
                            backoff_until=(meta["backoff_until"] if meta is not None else None))
        rules = parse_robots(row["content"] or "")
        match = rules.decide(path, user_agent)
        evidence = Evidence(
            host=host, path=path, user_agent=user_agent, allowed=match.allowed,
            reason=match.reason if match.allowed else "disallowed",
            version=row["version"], status="present", matched=match,
            fetched_at=row["fetched_at"], expires_at=row["expires_at"],
            http_status=row["http_status"], digest=row["digest"], stale=expired,
        )
        if expired:
            # Rules can still *deny* safely when stale, but a stale allow is
            # not permission to crawl.
            if not match.allowed:
                evidence.reason = "stale_disallowed"
                return evidence
            return self._stale_evidence(row, path, user_agent, expired=True, failure=failure,
                                        reason="stale_unverified", match=match)
        return evidence

    def _stale_evidence(self, row, path: str, user_agent: str, *, expired: bool, failure: bool,
                        reason: str, match: Optional[RuleMatch] = None) -> Evidence:
        return Evidence(
            host=row["host"], path=path, user_agent=user_agent, allowed=False,
            reason=reason if not failure else "expired_refresh_failed",
            version=row["version"], status=row["status"], matched=match,
            fetched_at=row["fetched_at"], expires_at=row["expires_at"],
            http_status=row["http_status"], digest=row["digest"], stale=expired,
            error=(self.meta(row["host"])["error"] if failure and self.meta(row["host"]) else None),
            backoff_until=(self.meta(row["host"])["backoff_until"] if self.meta(row["host"]) else None),
        )

    def crawl_delay(self, host: str, user_agent: str) -> Optional[float]:
        row = self.current(host, user_agent)
        if row is None or row["status"] != "present":
            return None
        return parse_robots(row["content"] or "").delay_for(user_agent)

    def versions(self, host: str, user_agent: Optional[str] = None) -> list[dict]:
        if user_agent is None:
            rows = self.state.query(
                "SELECT host,user_agent,version,status,fetched_at,expires_at,http_status FROM robots_cache "
                "WHERE host=? ORDER BY fetched_at DESC", (host,))
        else:
            rows = self.state.query(
                "SELECT host,user_agent,version,status,fetched_at,expires_at,http_status FROM robots_cache "
                "WHERE host=? AND user_agent=? ORDER BY fetched_at DESC", (host, user_agent))
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Legacy pipeline stage
# ---------------------------------------------------------------------------
def process(record):
    """Legacy single-responsibility stage.

    The legacy record pipeline has no URL/robots context.  It keeps its
    historical marker and, when a record carries a ``url`` plus an explicit
    ``robots`` decision, copies that evidence through unchanged.
    """
    rec = dict(record)
    rec.setdefault("_stage_robots", "ok")
    if isinstance(rec.get("robots"), dict):
        rec["robots_allowed"] = bool(rec["robots"].get("allowed", False))
    return rec


__all__ = [
    "Rule",
    "RuleMatch",
    "RobotsRules",
    "FetchResponse",
    "Evidence",
    "RobotsStore",
    "parse_robots",
    "process",
    "DEFAULT_TTL",
    "DEFAULT_BACKOFF",
]
