"""Rule admission: versioned rule sets combined with robots evidence.

Admission is a pure, explainable decision: every result lists the checks that
ran, the versioned evidence each check used, and a single reason for the final
verdict.  Ordering matters:

1. URL shape (scheme/path) — invalid/unsupported URLs are denied.
2. Scope rules (host allow/deny, path allow/deny, max depth, query policy).
3. robots.txt evidence for ``(host, path, user-agent)`` — **a denial here is
   absolute**.  A site priority, a quota or a deadline can never override it.
4. Default.  The default is *deny* when rules are missing or robots evidence
   is missing/stale/failed: absence of evidence is never permission.

Rule sets are versioned by content digest.  When a new version is activated,
pending (not yet executed) work that the new version forbids is reported by
:meth:`PolicyEngine.invalidate`; the planner releases its budget reservation
and concurrency slot.  Work that already executed, or whose outcome is unknown,
is never refunded — the decision method itself has no authority over the
ledger.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import urls as urlmod
from .clock import resolve_now


class RuleError(ValueError):
    """Raised when a rule document is structurally invalid."""


@dataclass(frozen=True)
class PathRule:
    field: str          # "allow" | "disallow"
    pattern: str
    line: int = 0


@dataclass
class RuleSet:
    """A versioned admission policy document.

    Accepted input (dict)::

        {
          "hosts": ["example.com"],          # optional allow-list
          "deny_hosts": ["ads.example.com"],
          "allow": ["/docs/*"],              # optional path allow-list
          "disallow": ["/admin/*", "/tmp"],
          "allow_query": true,
          "max_depth": 5,
          "default": "deny",                 # "deny" | "allow"
          "user_agent": "robotscope"
        }
    """

    version: str
    doc: dict
    created_at: float = 0.0
    active: bool = False

    @classmethod
    def from_doc(cls, doc: Any, *, created_at: float = 0.0) -> "RuleSet":
        if isinstance(doc, RuleSet):
            return doc
        if doc is None:
            doc = {}
        if not isinstance(doc, dict):
            raise RuleError("rule document must be a mapping")
        canonical = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        version = "p:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        # Validate eagerly so a bad document never becomes the active policy.
        for field_name in ("hosts", "deny_hosts", "allow", "disallow"):
            value = doc.get(field_name, [])
            if isinstance(value, str):
                value = [value]
            if value is None:
                value = []
            if not isinstance(value, (list, tuple)):
                raise RuleError(f"{field_name} must be a list")
            for item in value:
                if not isinstance(item, str):
                    raise RuleError(f"{field_name} entries must be strings")
        default = doc.get("default", "deny")
        if default not in ("allow", "deny"):
            raise RuleError("default must be 'allow' or 'deny'")
        max_depth = doc.get("max_depth")
        if max_depth is not None and (not isinstance(max_depth, int) or max_depth < 0):
            raise RuleError("max_depth must be a non-negative integer")
        return cls(version=version, doc=dict(doc), created_at=float(created_at))

    # -- accessors ------------------------------------------------------
    def host_list(self, key: str) -> list[str]:
        value = self.doc.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(v).lower() for v in value]

    def path_rules(self) -> list[PathRule]:
        rules = []
        for i, pattern in enumerate(self._list("allow")):
            rules.append(PathRule("allow", str(pattern), i))
        for i, pattern in enumerate(self._list("disallow")):
            rules.append(PathRule("disallow", str(pattern), 1000 + i))
        return rules

    def _list(self, key: str) -> list:
        value = self.doc.get(key) or []
        if isinstance(value, str):
            return [value]
        return list(value)

    @property
    def default(self) -> str:
        return self.doc.get("default", "deny")

    @property
    def user_agent(self) -> Optional[str]:
        return self.doc.get("user_agent")

    @property
    def max_depth(self) -> Optional[int]:
        return self.doc.get("max_depth")

    def as_dict(self) -> dict:
        return {"version": self.version, "doc": self.doc, "created_at": self.created_at,
                "active": self.active}


@dataclass
class Check:
    name: str
    allowed: bool
    reason: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "allowed": self.allowed, "reason": self.reason, "detail": self.detail}


@dataclass
class Decision:
    url: str
    allowed: bool
    reason: str
    policy_version: Optional[str]
    checks: list[Check] = field(default_factory=list)
    robots: Optional[dict] = None
    host: Optional[str] = None
    path: Optional[str] = None
    user_agent: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "host": self.host,
            "path": self.path,
            "user_agent": self.user_agent,
            "allowed": self.allowed,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "checks": [c.as_dict() for c in self.checks],
            "robots": self.robots,
        }


def _match_pattern(pattern: str, path: str) -> bool:
    if pattern == "":
        return True
    import re
    regex = ""
    for ch in pattern:
        if ch == "*":
            regex += ".*"
        else:
            regex += re.escape(ch)
    if pattern.endswith("$"):
        return re.fullmatch(regex[:-2] + "$", path) is not None
    return re.match(regex, path) is not None


class PolicyEngine:
    """Persist, activate and apply versioned rule sets."""

    def __init__(self, state, *, active: Optional[RuleSet] = None) -> None:
        self.state = state
        self._active: Optional[RuleSet] = None
        self._active_version: Optional[str] = None
        if active is not None:
            self.activate(active)

    @property
    def active(self) -> Optional[RuleSet]:
        """The active rule set, always consistent with persisted state.

        Two engines may hold the same state (e.g. a worker and an operator
        process); reading through the persisted version keeps them from
        diverging after one of them activates a new policy.
        """
        persisted = self.state.get_meta("active_policy_version", None)
        if persisted is None:
            row = self.state.one(
                "SELECT version FROM policies WHERE active=1 ORDER BY created_at DESC LIMIT 1")
            persisted = row["version"] if row is not None else ""
        if persisted != (self._active_version or ""):
            self._load_active()
        return self._active

    @active.setter
    def active(self, value: Optional[RuleSet]) -> None:
        self._active = value
        self._active_version = value.version if value is not None else ""

    def _load_active(self) -> Optional[RuleSet]:
        row = self.state.one("SELECT * FROM policies WHERE active=1 ORDER BY created_at DESC LIMIT 1")
        if row is None:
            self.active = None
            return None
        rule_set = RuleSet(version=row["version"], doc=json.loads(row["doc"]),
                           created_at=float(row["created_at"]), active=True)
        self.active = rule_set
        return rule_set

    def register(self, doc: Any, *, now: Optional[float] = None) -> RuleSet:
        now = resolve_now(self.state.clock, now)
        rule_set = RuleSet.from_doc(doc, created_at=now)
        existing = self.state.one("SELECT version FROM policies WHERE version=?", (rule_set.version,))
        if existing is None:
            with self.state.transaction():
                self.state.execute(
                    "INSERT INTO policies(version,doc,created_at,active) VALUES(?,?,?,0)",
                    (rule_set.version, json.dumps(rule_set.doc, ensure_ascii=False, sort_keys=True), now),
                )
        return rule_set

    def activate(self, doc: Any, *, now: Optional[float] = None,
                 previous: Optional[RuleSet] = None) -> tuple[RuleSet, list[dict]]:
        """Activate a rule document.

        Returns ``(new_ruleset, invalidation)`` where ``invalidation`` lists
        pending items that the new version forbids (computed by the caller via
        :meth:`invalidate`).  The old version remains queryable for audit.
        """
        now = resolve_now(self.state.clock, now)
        rule_set = self.register(doc, now=now)
        old = previous if previous is not None else getattr(self, "active", None)
        with self.state.transaction():
            self.state.execute("UPDATE policies SET active=0 WHERE active=1")
            self.state.execute("UPDATE policies SET active=1 WHERE version=?", (rule_set.version,))
            self.state.set_meta("active_policy_version", rule_set.version)
        rule_set.active = True
        self.active = rule_set
        self.state.set_json("policy", "active", {
            "version": rule_set.version,
            "previous": old.version if old is not None else None,
            "activated_at": now,
        })
        self.state.event("policy_activated", detail={
            "version": rule_set.version,
            "previous": old.version if old is not None else None,
        }, at=now)
        return rule_set, []

    def versions(self) -> list[dict]:
        return [dict(r) for r in self.state.query(
            "SELECT version,created_at,active FROM policies ORDER BY created_at DESC, version")]

    # -- decisions ------------------------------------------------------
    def check(self, url: str, *, user_agent: Optional[str] = None,
              robots: Optional[Any] = None, depth: int = 0,
              now: Optional[float] = None,
              rules: Optional[RuleSet] = None) -> Decision:
        """Decide admission.

        ``robots`` may be an ``Evidence`` object, a mapping of its
        ``as_dict()`` form, or ``None``.  ``None`` means "no evidence was
        supplied" and is denied unless the active policy explicitly says
        robots may be skipped via ``"robots": "optional"``.

        ``rules`` overrides the active rule set (used to evaluate a *candidate*
        policy before activation).
        """
        now = resolve_now(self.state.clock, now)
        rules = rules if rules is not None else getattr(self, "active", None)
        policy_version = rules.version if rules is not None else None
        checks: list[Check] = []
        try:
            normalized = urlmod.normalize(url)
        except urlmod.URLError as exc:
            return Decision(str(url), False, "invalid_url", policy_version,
                            checks=[Check("shape", False, "invalid_url", {"error": str(exc)})])
        host = urlmod.host_of(normalized)
        path = urlmod.path_of(normalized)

        if rules is None:
            return Decision(normalized, False, "no_policy", None, host=host, path=path,
                            user_agent=user_agent,
                            checks=[Check("policy", False, "no_policy")])

        # 1. scope
        scope = self._scope_checks(rules, host, path, depth)
        checks.extend(scope)
        scope_denied = [c for c in scope if not c.allowed]
        # 2. robots
        ua = user_agent or rules.user_agent or "robotscope"
        robots_check, robots_dict = self._robots_check(rules, robots, host, path, ua)
        checks.append(robots_check)
        robots_denied = not robots_check.allowed

        if robots_denied:
            reason = robots_check.reason if robots_check.reason != "robots_missing" else "robots_unavailable"
            # Robots is absolute: even a scope allow cannot override it.
            return Decision(normalized, False, reason, policy_version, checks,
                            robots=robots_dict, host=host, path=path, user_agent=ua)
        if scope_denied:
            return Decision(normalized, False, scope_denied[0].reason, policy_version, checks,
                            robots=robots_dict, host=host, path=path, user_agent=ua)
        return Decision(normalized, True, "allowed", policy_version, checks,
                        robots=robots_dict, host=host, path=path, user_agent=ua)

    def _scope_checks(self, rules: RuleSet, host: str, path: str, depth: int) -> list[Check]:
        checks = []
        hosts = rules.host_list("hosts")
        if hosts:
            allowed = host in hosts
            checks.append(Check("host_allowlist", allowed, "host_allowed" if allowed else "host_not_allowed",
                                {"host": host, "hosts": hosts}))
        deny_hosts = rules.host_list("deny_hosts")
        if deny_hosts and host in deny_hosts:
            checks.append(Check("host_denylist", False, "host_denied", {"host": host}))
        if rules.max_depth is not None and depth > rules.max_depth:
            checks.append(Check("depth", False, "depth_exceeded",
                                {"depth": depth, "max_depth": rules.max_depth}))
        best: Optional[PathRule] = None
        best_len = -1
        for rule in rules.path_rules():
            if _match_pattern(rule.pattern, path):
                # Allow wins ties, matching RFC 9309 semantics.
                if len(rule.pattern) > best_len or (
                    len(rule.pattern) == best_len and rule.field == "allow"
                ):
                    best = rule
                    best_len = len(rule.pattern)
        if best is not None:
            allowed = best.field == "allow"
            checks.append(Check("path_rules", allowed, "allowed" if allowed else "path_disallowed",
                                {"pattern": best.pattern, "field": best.field}))
        if rules.default == "deny" and best is None:
            checks.append(Check("default", False, "not_allowlisted"))
        return checks

    def _robots_check(self, rules: RuleSet, robots: Any, host: str, path: str,
                      user_agent: str) -> tuple[Check, Optional[dict]]:
        if robots is None:
            if rules.doc.get("robots") == "optional":
                return Check("robots", True, "robots_optional"), None
            return Check("robots", False, "robots_missing"), None
        if hasattr(robots, "as_dict"):
            data = robots.as_dict()
        elif isinstance(robots, dict):
            data = dict(robots)
        else:
            return Check("robots", False, "robots_invalid"), {"value": repr(robots)}
        allowed = bool(data.get("allowed"))
        reason = str(data.get("reason") or ("allowed" if allowed else "robots_denied"))
        detail = {
            "version": data.get("version"),
            "status": data.get("status"),
            "stale": data.get("stale"),
            "host": data.get("host"),
            "path": data.get("path"),
            "user_agent": data.get("user_agent"),
        }
        if data.get("host") is not None and data.get("host") != host:
            # Evidence for another host is not evidence for this one.
            return Check("robots", False, "robots_host_mismatch", detail), data
        if data.get("path") is not None and data.get("path") != path:
            # Decision evidence is per path; a verdict for /a says nothing
            # about /b, even on the same host.
            return Check("robots", False, "robots_path_mismatch", detail), data
        if data.get("user_agent") is not None and user_agent and data["user_agent"] != user_agent:
            return Check("robots", False, "robots_user_agent_mismatch", detail), data
        if data.get("stale"):
            # A stale allow is not permission, even if path rules would allow.
            return Check("robots", False, "robots_stale", detail), data
        return Check("robots", allowed, reason, detail), data

    # -- invalidation ---------------------------------------------------
    def invalidate(self, pending: Iterable[dict], *, rules: Optional[RuleSet] = None,
                   user_agent: Optional[str] = None, now: Optional[float] = None) -> list[dict]:
        """Find pending items the active rules now forbid.

        ``pending`` items are dicts with at least ``url``; optional ``robots``
        evidence, ``depth``, ``state`` and ``request_id`` are honoured.  Only
        items whose state is still pending/queued are returned — the planner is
        responsible for releasing *their* budget reservations.  Items marked
        executed/in_flight/unknown are skipped by design.
        """
        rules = rules or getattr(self, "active", None)
        if rules is None:
            return []
        invalid = []
        for item in pending:
            state = str(item.get("state", "pending"))
            if state in ("executed", "in_flight", "unknown", "spent", "failed"):
                continue
            decision = self.check(item.get("url", ""), user_agent=user_agent or item.get("user_agent"),
                                  robots=item.get("robots"), depth=int(item.get("depth", 0) or 0),
                                  now=now, rules=rules)
            if not decision.allowed:
                invalid.append({
                    "url": decision.url,
                    "request_id": item.get("request_id"),
                    "host": decision.host,
                    "reason": decision.reason,
                    "policy_version": decision.policy_version,
                    "previous_policy_version": item.get("policy_version"),
                    "decision": decision.as_dict(),
                })
        return invalid


# ---------------------------------------------------------------------------
# Legacy pipeline stage
# ---------------------------------------------------------------------------
def process(record):
    """Legacy single-responsibility stage.

    The legacy record pipeline has no URL/robots context.  Records carrying an
    explicit ``policy``/``robots`` decision are annotated; everything else is
    passed through with the stage marker.  No permission is invented.
    """
    rec = dict(record)
    rec.setdefault("_stage_policy_engine", "ok")
    if isinstance(rec.get("policy"), dict):
        rec["policy_allowed"] = bool(rec["policy"].get("allowed", False))
    elif isinstance(rec.get("robots"), dict):
        rec["policy_allowed"] = bool(rec["robots"].get("allowed", False))
        rec.setdefault("policy_reason", rec["robots"].get("reason"))
    return rec


__all__ = [
    "RuleError",
    "PathRule",
    "RuleSet",
    "Check",
    "Decision",
    "PolicyEngine",
    "process",
]
