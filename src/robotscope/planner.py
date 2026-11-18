"""Crawl planner: discovery → admission → ranking → budget → host → slot.

The planner is the only place that coordinates the domains.  Each domain
module remains answerable on its own:

* discovery (:mod:`robotscope.sitemap`) reports what was found and whether the
  scan is complete;
* admission (:mod:`robotscope.policy_engine`, :mod:`robotscope.robots`) says
  yes/no with versioned evidence;
* ordering (:mod:`robotscope.value_rank`) says what is worth doing next;
* quota (:mod:`robotscope.budget`), host pacing
  (:mod:`robotscope.politeness`) and worker slots
  (:mod:`robotscope.lease`) are accounted **separately**.

Guarantees
----------
* ``plan`` is a pure projection: it never fetches pages, never reserves quota
  and never takes a slot.
* ``dispatch`` reserves budget (idempotent per request id), enforces the host
  wait and acquires exactly one slot, in one order that can be explained.
* ``apply_policy`` invalidates pending work and releases *refundable*
  reservations and slots; executed/unknown work keeps its charge.
* ``recover`` reclaims expired leases and converts in-flight uncertainty into
  an ``unknown`` charge rather than a free retry.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from . import urls as urlmod
from . import value_rank
from . import crawl_config
from .budget import Budget
from .clock import day_of, resolve_now
from .lease import Lease, LeaseManager
from .policy_engine import Decision, PolicyEngine
from .politeness import Politeness
from .robots import RobotsStore
from .sitemap import Discovery, SitemapWalker, deletion_candidates

ROBOTS_UNAVAILABLE_REASONS = frozenset({
    "cache_missing", "fetch_failed", "stale_unverified", "expired_refresh_failed",
    "robots_missing", "robots_stale", "robots_path_mismatch", "robots_host_mismatch",
    "robots_invalid",
})

# Terminal / non-actionable states.  ``unknown`` is terminal until a receipt
# arrives; it is *charged*, never silently retried for free.
TERMINAL = ("executed", "failed", "unknown", "denied", "invalidated", "cancelled")


def robots_unavailable(evidence: Optional[dict]) -> Optional[str]:
    """Return the reason robots evidence is unusable, or ``None`` if it is usable.

    A usable evidence object may still be an explicit denial; this helper only
    answers "can we make a decision at all?".
    """
    if not isinstance(evidence, dict):
        return "cache_missing"
    if evidence.get("allowed") is True:
        return None
    if evidence.get("allowed") is False:
        reason = str(evidence.get("reason") or "")
        if reason in ROBOTS_UNAVAILABLE_REASONS:
            return reason
        if evidence.get("status") in ("missing", "error") or evidence.get("stale"):
            return reason or "stale_unverified"
        return None  # an explicit disallow
    return "cache_missing"


def request_id_for(crawl_id: str, url: str) -> str:
    """Stable request identity: re-planning the same URL does not double-book."""
    digest = hashlib.sha256(f"{crawl_id}\n{url}".encode("utf-8")).hexdigest()[:20]
    return f"q-{digest}"


@dataclass
class Item:
    crawl_id: str
    url: str
    request_id: str
    host: str
    state: str = "discovered"
    depth: int = 0
    source: Optional[str] = None
    priority: float = 0.0
    cost: float = 1.0
    deadline: Optional[float] = None
    historical_score: float = 0.0
    enqueued_at: float = 0.0
    last_served_at: Optional[float] = None
    policy_version: Optional[str] = None
    robots: Optional[dict] = None
    reason: Optional[str] = None
    lease_id: Optional[str] = None
    fence: Optional[int] = None
    lastmod: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def candidate(self) -> value_rank.Candidate:
        return value_rank.Candidate(
            url=self.url, priority=self.priority, historical_score=self.historical_score,
            cost=self.cost, deadline=self.deadline, enqueued_at=self.enqueued_at,
            last_served_at=self.last_served_at, host=self.host, tie=self.request_id,
        )

    def payload(self) -> dict:
        data = {
            "request_id": self.request_id, "depth": self.depth, "source": self.source,
            "priority": self.priority, "cost": self.cost, "deadline": self.deadline,
            "historical_score": self.historical_score, "enqueued_at": self.enqueued_at,
            "last_served_at": self.last_served_at, "policy_version": self.policy_version,
            "robots": self.robots, "reason": self.reason, "lease_id": self.lease_id,
            "fence": self.fence, "lastmod": self.lastmod,
        }
        data.update(self.extra)
        return data

    @property
    def attempt(self) -> int:
        return int(self.extra.get("attempt", 1) or 1)

    def budget_key(self) -> str:
        """Ledger key for the *current attempt*.

        The first attempt uses the stable request id, so duplicate dispatches
        deduplicate.  A genuine retry gets its own key: the origin may already
        have been contacted, so the retry must be charged in addition to
        (never instead of) the earlier attempt.
        """
        if self.attempt <= 1:
            return self.request_id
        return f"{self.request_id}#a{self.attempt}"

    def next_attempt(self) -> str:
        self.extra = {**self.extra, "attempt": self.attempt + 1}
        return self.budget_key()

    @classmethod
    def from_row(cls, row) -> "Item":
        try:
            data = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            data = {}
        known = {f: data.get(f) for f in (
            "request_id", "depth", "source", "priority", "cost", "deadline",
            "historical_score", "enqueued_at", "last_served_at", "policy_version",
            "robots", "reason", "lease_id", "fence", "lastmod")}
        known["request_id"] = known["request_id"] or request_id_for(row["crawl_id"], row["url"])
        known["depth"] = int(known["depth"] or 0)
        known["priority"] = float(known["priority"] or 0.0)
        known["cost"] = float(known["cost"] or 1.0)
        known["historical_score"] = float(known["historical_score"] or 0.0)
        known["enqueued_at"] = float(known["enqueued_at"] or 0.0)
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(crawl_id=row["crawl_id"], url=row["url"], host=row["host"] or urlmod.host_of(row["url"]),
                   state=row["state"], **known, extra=extra)

    def as_dict(self) -> dict:
        return {
            "crawl_id": self.crawl_id, "url": self.url, "request_id": self.request_id,
            "host": self.host, "state": self.state, "reason": self.reason,
            "depth": self.depth, "source": self.source, "priority": self.priority,
            "cost": self.cost, "deadline": self.deadline,
            "historical_score": self.historical_score, "enqueued_at": self.enqueued_at,
            "policy_version": self.policy_version, "robots": self.robots,
            "lease_id": self.lease_id, "fence": self.fence, "lastmod": self.lastmod,
        }


@dataclass
class DispatchResult:
    request_id: str
    url: str
    granted: bool
    reason: str
    lease: Optional[Lease] = None
    budget: Optional[dict] = None
    retry_at: Optional[float] = None
    policy_version: Optional[str] = None
    duplicate: bool = False

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id, "url": self.url, "granted": self.granted,
            "reason": self.reason, "retry_at": self.retry_at,
            "policy_version": self.policy_version, "duplicate": self.duplicate,
            "lease": self.lease.as_dict() if self.lease else None,
            "budget": self.budget,
        }


class Planner:
    """Coordinates one crawl scope.

    ``crawl_id`` namespaces discoveries so two crawls can share a database.
    """

    def __init__(self, state, *, crawl_id: str = "crawl", user_agent: str = "robotscope",
                 rules: Optional[Any] = None, limits: Optional[Any] = None,
                 min_interval: float = 0.0, global_slots: Optional[int] = None,
                 host_slots: Optional[int] = None, lease_ttl: float = 120.0,
                 robots_ttl: float = 3600.0, reservation_ttl: float = 900.0) -> None:
        self.state = state
        self.crawl_id = str(crawl_id)
        self.user_agent = user_agent
        self.policy = PolicyEngine(state)
        if rules is not None:
            self.policy.activate(rules)
        elif self.policy.active is None:
            self.policy.activate({})
        self.robots = RobotsStore(state, ttl=robots_ttl)
        self.budget = Budget(state, limits=limits, reservation_ttl=reservation_ttl)
        self.politeness = Politeness(state, default_interval=min_interval)
        self.leases = LeaseManager(state, global_limit=global_slots, host_limit=host_slots,
                                   lease_ttl=lease_ttl)
        self.state.set_json("planner", "config", {
            "crawl_id": self.crawl_id, "user_agent": self.user_agent,
            "global_slots": global_slots, "host_slots": host_slots,
            "min_interval": min_interval,
        })

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def discover(self, roots: Iterable[str], *, fetcher: Optional[Callable[..., Any]] = None,
                 base: Optional[str] = None, now: Optional[float] = None,
                 depth: int = 0, source: Optional[str] = None,
                 extra_robots_sitemaps: bool = True) -> dict:
        """Walk sitemap roots and register every discovered URL.

        Returns a summary including the discovery object, whether the scan is
        complete, and how many URLs were newly added.  Deletion candidates are
        only computed from ``complete`` scans.
        """
        now = resolve_now(self.state.clock, now)
        discovery = SitemapWalker(state=self.state, fetcher=fetcher).walk(
            roots, base=base, now=now)
        if extra_robots_sitemaps:
            for row in self.state.query(
                    "SELECT host FROM robots_meta WHERE status='present'"):
                meta = self.state.one("SELECT content FROM robots_meta WHERE host=?", (row["host"],))
                if meta is None:
                    continue
                from .robots import parse_robots
                for sitemap_url in parse_robots(meta["content"] or "").sitemaps:
                    if sitemap_url not in discovery.documents:
                        more = SitemapWalker(state=self.state, fetcher=fetcher).walk(
                            [sitemap_url], now=now)
                        for url, entry in more.entries.items():
                            discovery.entries.setdefault(url, entry)
                        for url, doc in more.documents.items():
                            discovery.documents.setdefault(url, doc)
                        discovery.failures.extend(more.failures)
                        discovery.complete = discovery.complete and more.complete
        added = 0
        for url, entry in discovery.entries.items():
            item = Item(
                crawl_id=self.crawl_id, url=url,
                request_id=request_id_for(self.crawl_id, url),
                host=urlmod.host_of(url), state="discovered", depth=depth,
                source=source or (entry.sources[0] if entry.sources else None),
                lastmod=entry.lastmod,
                priority=float(entry.priority or 0.0),
                enqueued_at=now,
            )
            existing = self._load(url)
            if existing is None:
                self._save(item, now=now)
                added += 1
            else:
                # Discovery refreshes metadata only.  A denial is a decision,
                # not a discovery fact: re-seeing the URL in a sitemap does
                # not lift the robots/rule denial.  State transitions happen
                # in ``assess`` or ``apply_policy``.
                existing.lastmod = existing.lastmod or entry.lastmod
                if entry.priority is not None:
                    existing.priority = max(existing.priority, float(entry.priority))
                existing.source = existing.source or (entry.sources[0] if entry.sources else None)
                self._save(existing, now=now)
        known = {row["url"] for row in self.state.query(
            "SELECT url FROM crawl_urls WHERE crawl_id=?", (self.crawl_id,))}
        deletions = deletion_candidates(discovery, known)
        self.state.set_json("discovery", self.crawl_id, {
            "roots": list(discovery.roots),
            "complete": discovery.complete,
            "truncated": discovery.truncated,
            "documents": len(discovery.documents),
            "entries": len(discovery.entries),
            "failures": discovery.failures,
            "cycle_edges": [list(e) for e in discovery.cycle_edges],
            "shared_edges": [list(e) for e in discovery.shared_edges],
            "deletions": deletions,
            "at": now,
        })
        self.state.event("discovery_completed", detail={
            "crawl_id": self.crawl_id, "roots": list(discovery.roots),
            "complete": discovery.complete, "added": added,
            "failures": len(discovery.failures),
        }, at=now)
        return {
            "discovery": discovery,
            "complete": discovery.complete,
            "truncated": discovery.truncated,
            "added": added,
            "known": len(known),
            "deletions": deletions,
            "summary": discovery.as_dict(),
        }

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def add_url(self, url: str, *, now: Optional[float] = None,
                priority: float = 0.0, deadline: Optional[float] = None,
                historical_score: float = 0.0, cost: float = 1.0,
                depth: int = 0, source: Optional[str] = None) -> Item:
        """Register one URL (seed or page link) without fetching it."""
        now = resolve_now(self.state.clock, now)
        normalized = urlmod.normalize(url)
        existing = self._load(normalized)
        if existing is not None:
            return existing
        item = Item(
            crawl_id=self.crawl_id, url=normalized,
            request_id=request_id_for(self.crawl_id, normalized),
            host=urlmod.host_of(normalized), state="discovered", depth=depth,
            source=source, priority=float(priority or 0.0),
            cost=float(cost or 1.0), deadline=deadline,
            historical_score=float(historical_score or 0.0), enqueued_at=now,
        )
        self._save(item, now=now)
        return item

    def _load(self, url: str) -> Optional[Item]:
        row = self.state.one("SELECT * FROM crawl_urls WHERE crawl_id=? AND url=?",
                             (self.crawl_id, url))
        return Item.from_row(row) if row is not None else None

    def _save(self, item: Item, *, now: Optional[float] = None) -> None:
        now = resolve_now(self.state.clock, now)
        with self.state.transaction():
            self.state.execute(
                "INSERT INTO crawl_urls(crawl_id,url,payload,state,host,policy_version,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(crawl_id,url) DO UPDATE SET "
                "payload=excluded.payload,state=excluded.state,host=excluded.host,"
                "policy_version=excluded.policy_version,updated_at=excluded.updated_at",
                (item.crawl_id, item.url, json.dumps(item.payload(), ensure_ascii=False, sort_keys=True),
                 item.state, item.host, item.policy_version, now),
            )

    def items(self, states: Optional[Iterable[str]] = None) -> list[Item]:
        if states is None:
            rows = self.state.query(
                "SELECT * FROM crawl_urls WHERE crawl_id=? ORDER BY url", (self.crawl_id,))
        else:
            states = list(states)
            if not states:
                return []
            rows = self.state.query(
                "SELECT * FROM crawl_urls WHERE crawl_id=? AND state IN (%s) ORDER BY url" %
                ",".join("?" for _ in states), (self.crawl_id, *states))
        return [Item.from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------
    def assess(self, items: Optional[Iterable[Item]] = None, *,
               robots_fetcher: Optional[Callable[..., Any]] = None,
               now: Optional[float] = None, refresh_robots: bool = False) -> list[Item]:
        """Attach robots evidence and an admission decision to every item.

        Ordering is robots first; a denial is recorded with its versioned
        evidence.  When a robots fetch is impossible the item is deferred (not
        allowed) and keeps ``cache_missing`` / ``fetch_failed`` as its reason.
        """
        now = resolve_now(self.state.clock, now)
        items = list(items) if items is not None else self.items(
            states=("discovered", "queued", "waiting_host", "waiting_budget", "deferred"))
        for item in items:
            evidence = self.robots.check(
                item.url, self.user_agent, now=now,
                refresh=refresh_robots and robots_fetcher is not None,
                fetcher=robots_fetcher,
            )
            item.robots = evidence.as_dict()
            delay = self.robots.crawl_delay(item.host, self.user_agent)
            if delay is not None:
                self.politeness.configure_interval(item.host, delay, now=now)
            decision = self.policy.check(item.url, user_agent=self.user_agent,
                                         robots=evidence, depth=item.depth, now=now)
            item.policy_version = decision.policy_version
            unavailable = robots_unavailable(evidence.as_dict())
            if decision.allowed:
                if item.state in ("denied", "invalidated"):
                    item.state = "discovered"
                item.reason = "allowed"
            elif unavailable is not None and decision.reason in (
                    "robots_unavailable", "robots_missing", "robots_stale",
                    "robots_path_mismatch", "robots_host_mismatch", "robots_invalid",
                    "fetch_failed", "cache_missing"):
                # No usable evidence is not a verdict: keep the item pending so
                # a later refresh can admit it.  It is never marked crawlable.
                item.state = "deferred"
                item.reason = unavailable
            else:
                item.state = "denied"
                item.reason = decision.reason
            self._save(item, now=now)
            self.state.event("admission", request_id=item.request_id, detail={
                "url": item.url, "allowed": decision.allowed, "reason": decision.reason,
                "policy_version": decision.policy_version,
                "robots_version": evidence.version,
            }, at=now)
        return items

    def admission_for(self, url: str, *, robots_evidence: Optional[Any] = None,
                      depth: int = 0, now: Optional[float] = None) -> Decision:
        return self.policy.check(url, user_agent=self.user_agent,
                                 robots=robots_evidence, depth=depth, now=now)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    PENDING_STATES = ("discovered", "queued", "waiting_host", "waiting_budget",
                      "waiting_slot", "deferred")

    def _dispatchable(self, item: Item) -> bool:
        return item.state in self.PENDING_STATES

    def plan(self, *, now: Optional[float] = None, limit: Optional[int] = None,
             weights: Optional[dict] = None, starvation_window: float = 600.0,
             reserve: bool = False) -> dict:
        """Project the next batch without side effects.

        Buckets are mutually exclusive and always sum to all known URLs, so
        "what is discovered / denied / waiting / left after quota" can be read
        directly from the report.
        """
        now = resolve_now(self.state.clock, now)
        items = self.items()
        ready: list[value_rank.Ranked] = []
        waiting_host: list[dict] = []
        waiting_budget: list[dict] = []
        waiting_slot: list[dict] = []
        unassessed: list[dict] = []
        denied: list[dict] = []
        done: list[dict] = []
        invalidated: list[dict] = []
        other: list[dict] = []

        # Budget headroom for projection; reservation still happens at dispatch.
        day = day_of(now)
        remaining = self.budget.remaining(day, now=now)
        projected = 0.0
        ranked = value_rank.rank([i.candidate() for i in items if self._dispatchable(i)],
                                 now=now, weights=weights, starvation_window=starvation_window)
        by_url = {i.url: i for i in items}
        slot_usage = self.leases.slot_usage()
        global_limit = self.leases.policy.global_limit
        host_limit = self.leases.policy.host_limit
        projected_slots_global = slot_usage["global"]
        projected_slots_host = dict(slot_usage["by_host"])
        for entry in ranked:
            item = by_url[entry.candidate.url]
            host_state = self.politeness.state_of(item.host)
            host_ready_at = max(host_state.next_allowed_at, host_state.wait_until)
            record = {
                "request_id": item.request_id, "url": item.url, "host": item.host,
                "rank": entry.rank, "score": round(entry.score, 6),
                "breakdown": entry.breakdown,
            }
            if item.state == "denied" or (item.robots and item.robots.get("allowed") is False
                                          and robots_unavailable(item.robots) is None):
                continue
            if item.robots is None or item.policy_version is None:
                unassessed.append({**record, "reason": item.reason or "not_assessed",
                                   "state": item.state})
                continue
            unavailable = robots_unavailable(item.robots)
            if unavailable is not None:
                # Missing / expired / failed evidence: conservative wait, no
                # quota and no slot are consumed.
                unassessed.append({**record, "reason": unavailable, "state": item.state})
                continue
            if host_state.wait_until > now:
                waiting_host.append({**record, "reason": host_state.wait_reason or "host_wait",
                                     "retry_at": host_state.wait_until,
                                     "remaining": host_state.wait_until - now})
                continue
            if host_ready_at > now:
                waiting_host.append({**record, "reason": "pacing", "retry_at": host_ready_at,
                                     "remaining": host_ready_at - now})
                continue
            if remaining is not None and projected + item.cost > remaining:
                waiting_budget.append({**record, "reason": "budget_exhausted",
                                       "retry_at": next_reset(now),
                                       "cost": item.cost,
                                       "remaining": max(0.0, remaining - projected)})
                continue
            # Slots are a concurrency resource, separate from quota and host
            # pacing.  Project them in ranked order.
            host_in_use = projected_slots_host.get(item.host, 0)
            if global_limit is not None and projected_slots_global >= global_limit:
                waiting_slot.append({**record, "reason": "global_slots_full",
                                     "limit": global_limit,
                                     "in_use": projected_slots_global})
                continue
            if host_limit is not None and host_in_use >= host_limit:
                waiting_slot.append({**record, "reason": "host_slots_full",
                                     "limit": host_limit, "in_use": host_in_use})
                continue
            projected += item.cost
            projected_slots_global += 1
            projected_slots_host[item.host] = host_in_use + 1
            ready.append(entry)

        for item in items:
            record = {"request_id": item.request_id, "url": item.url, "host": item.host,
                      "reason": item.reason, "state": item.state}
            if item.state == "denied":
                denied.append(record)
            elif item.state in ("executed", "failed", "unknown", "cancelled"):
                done.append(record)
            elif item.state == "invalidated":
                invalidated.append(record)
            elif item.state in ("leased", "in_flight"):
                other.append(record)
            elif item.state == "deferred" and item.reason in ROBOTS_UNAVAILABLE_REASONS:
                # Deferred for missing evidence is a subset of the unassessed
                # bucket; keep bucket counts disjoint.
                if not any(entry["request_id"] == item.request_id for entry in unassessed):
                    unassessed.append(record)

        coverage = self._coverage(items, now=now)
        ready_records = [_ranked_record(r) for r in ready]
        batch = ready_records[:limit] if limit is not None else ready_records
        deferred = ready_records[limit:] if limit is not None else []
        report = {
            "crawl_id": self.crawl_id,
            "generated_at": now,
            "day": day,
            "policy_version": self.policy.active.version if self.policy.active else None,
            "user_agent": self.user_agent,
            "counts": {
                "known": len(items),
                "ready": len(ready),
                "batch": len(batch),
                "waiting_host": len(waiting_host),
                "waiting_budget": len(waiting_budget),
                "waiting_slot": len(waiting_slot),
                "unassessed": len(unassessed),
                "denied": len(denied),
                "invalidated": len(invalidated),
                "in_flight": len(other),
                "finished": len(done),
            },
            "batch": batch,
            "deferred": deferred,
            "buckets": {
                "ready": ready_records,
                "waiting_host": waiting_host,
                "waiting_budget": waiting_budget,
                "waiting_slot": waiting_slot,
                "unassessed": unassessed,
                "denied": denied,
                "invalidated": invalidated,
                "in_flight": other,
                "finished": done,
            },
            "coverage": coverage,
            "budget": self.budget.summary(day, now=now),
            "pace": self.politeness.metric(now=now),
            "slots": self.leases.metric(),
            "tradeoff": crawl_config.describe(),
            "remaining_after_budget": [r["url"] for r in waiting_budget],
            "deferrals": [
                {"url": r["url"], "request_id": r["request_id"],
                 "reason": r["reason"], "retry_at": r.get("retry_at")}
                for r in waiting_host + waiting_budget + waiting_slot + unassessed
            ],
            "explanations": {
                "denied": "robots denial or rule exclusion; priority can never override it",
                "waiting_host": "host is pacing or in an externally imposed wait; no slot is held",
                "waiting_budget": "daily quota is exhausted; retry at next_reset_at",
                "waiting_slot": "concurrency slot unavailable (global or per-host); quota is untouched",
                "unassessed": "no versioned robots evidence yet; conservative wait",
                "remaining_after_budget": "URLs that would run once quota resets",
            },
            "next_reset_at": next_reset(now),
        }
        if reserve:
            report["reserved"] = [
                self.dispatch(r["request_id"], now=now).as_dict() for r in batch
            ]
        self.state.event("plan_computed", detail={
            "crawl_id": self.crawl_id, "counts": report["counts"], "day": day,
        }, at=now)
        return report

    def _coverage(self, items: list[Item], *, now: float) -> dict:
        discovery = self.state.get_json("discovery", self.crawl_id, {}) or {}
        states: dict[str, int] = {}
        for item in items:
            states[item.state] = states.get(item.state, 0) + 1
        total = len(items)
        pending_unassessed = sum(1 for i in items if robots_unavailable(i.robots) is not None)
        evaluated = total - pending_unassessed
        complete = bool(discovery.get("complete", False))
        return {
            "sitemap_complete": complete,
            "truncated": bool(discovery.get("truncated", False)),
            "roots": discovery.get("roots", []),
            "documents": discovery.get("documents", 0),
            "discovered": total,
            "by_state": states,
            "robots_pending": pending_unassessed,
            "admission_evaluated": max(0, evaluated),
            "coverage_ratio": (evaluated / total) if total else 1.0,
            "deletion_inference": "allowed" if complete else "blocked_incomplete_scan",
            "scan_failures": len(discovery.get("failures") or []),
            "cycle_edges": discovery.get("cycle_edges") or [],
            "shared_edges": discovery.get("shared_edges") or [],
        }

    # ------------------------------------------------------------------
    # Dispatch (budget + host + slot)
    # ------------------------------------------------------------------
    def dispatch(self, request_id: str, *, worker: Optional[str] = None,
                 now: Optional[float] = None, ttl: Optional[float] = None,
                 lease_ttl: Optional[float] = None) -> DispatchResult:
        """Authorise exactly one execution of *request_id*.

        Order of checks: admission evidence → host wait/pacing → budget →
        slot.  Budget is reserved before the slot so a slot is never held for
        a request that cannot be paid for.  The reservation is idempotent, so
        a retried dispatch does not consume quota twice.
        """
        now = resolve_now(self.state.clock, now)
        item = self._by_request(request_id)
        if item is None:
            return DispatchResult(request_id, "", False, "unknown_request")
        if item.state in ("executed", "failed", "unknown", "cancelled", "invalidated"):
            return DispatchResult(request_id, item.url, False, f"terminal_{item.state}",
                                  policy_version=item.policy_version)
        if item.state == "denied":
            return DispatchResult(request_id, item.url, False, f"denied:{item.reason}",
                                  policy_version=item.policy_version)
        if item.state == "leased":
            lease = self.leases.current(request_id)
            if lease is not None and self.leases.validate(
                    lease["lease_id"], request_id=request_id, fence=lease["fence"], now=now):
                return DispatchResult(request_id, item.url, True, "duplicate_lease",
                                      lease=Lease(lease_id=lease["lease_id"], request_id=request_id,
                                                  host=item.host, worker=lease["worker"],
                                                  fence=int(lease["fence"]), granted=True,
                                                  reason="duplicate_lease",
                                                  expires_at=lease["expires"],
                                                  duplicate=True),
                                      policy_version=item.policy_version, duplicate=True)
            # The stored lease is gone/expired.  The previous attempt may have
            # reached the origin; charge it as unknown rather than letting a
            # fresh reservation look like a first attempt.
            self.mark_unknown(request_id, reason="lease_lost_before_retry", now=now)
            item = self._by_request(request_id) or item
        if item.state == "unknown":
            return DispatchResult(request_id, item.url, False, "unknown_outcome",
                                  policy_version=item.policy_version)
        # Re-check admission with the *current* policy and stored evidence.
        robots = item.robots
        decision = self.policy.check(item.url, user_agent=self.user_agent,
                                     robots=robots, depth=item.depth, now=now)
        if not decision.allowed:
            if robots_unavailable(robots) is not None:
                # No usable evidence: this is a wait, not a denial.  Keep the
                # item pending so a later refresh can admit it.
                if item.state != "deferred":
                    item.state = "deferred"
                    item.reason = robots_unavailable(robots)
                    self._save(item, now=now)
                return DispatchResult(request_id, item.url, False,
                                      f"unassessed:{item.reason}",
                                      policy_version=decision.policy_version)
            item.state = "denied"
            item.reason = decision.reason
            item.policy_version = decision.policy_version
            self._save(item, now=now)
            return DispatchResult(request_id, item.url, False, f"denied:{decision.reason}",
                                  policy_version=decision.policy_version)
        host = item.host
        host_state = self.politeness.state_of(host)
        if host_state.wait_until > now:
            return DispatchResult(request_id, item.url, False,
                                  f"host_wait:{host_state.wait_reason or 'backoff'}",
                                  retry_at=host_state.wait_until,
                                  policy_version=decision.policy_version)
        if host_state.next_allowed_at > now:
            return DispatchResult(request_id, item.url, False, "host_pacing",
                                  retry_at=host_state.next_allowed_at,
                                  policy_version=decision.policy_version)
        reservation = self.budget.reserve(item.budget_key(), item.cost, host=host, now=now)
        if not reservation.granted:
            if reservation.state == "denied":
                item.state = "waiting_budget"
                item.reason = reservation.reason
                self._save(item, now=now)
            return DispatchResult(request_id, item.url, False, reservation.reason,
                                  retry_at=reservation.retry_at,
                                  budget=reservation.as_dict(),
                                  policy_version=decision.policy_version)
        lease = self.leases.acquire(request_id, host, worker=worker, now=now,
                                    ttl=lease_ttl if lease_ttl is not None else ttl)
        if not lease.granted:
            # Give the quota back: nothing was executed and this was still a
            # refundable reservation.
            self.budget.discard(item.budget_key(), now=now)
            item.state = "waiting_slot"
            item.reason = lease.reason
            self._save(item, now=now)
            return DispatchResult(request_id, item.url, False, lease.reason,
                                  budget=reservation.as_dict(),
                                  policy_version=decision.policy_version)
        self.budget.mark_in_flight(item.budget_key(), lease_id=lease.lease_id,
                                   fence=lease.fence, now=now)
        self.politeness.note_request(host, now=now,
                                     crawl_delay=self.robots.crawl_delay(host, self.user_agent))
        item.state = "leased"
        item.lease_id = lease.lease_id
        item.fence = lease.fence
        item.reason = "leased"
        item.policy_version = decision.policy_version
        self._save(item, now=now)
        self.state.event("dispatch_granted", request_id=request_id, detail={
            "url": item.url, "host": host, "lease_id": lease.lease_id, "fence": lease.fence,
            "cost": item.cost, "policy_version": decision.policy_version,
            "attempt": item.attempt, "budget_key": item.budget_key(),
        }, at=now)
        return DispatchResult(request_id, item.url, True, "dispatched", lease=lease,
                              budget=reservation.as_dict(),
                              policy_version=decision.policy_version)

    def _by_request(self, request_id: str) -> Optional[Item]:
        # Request ids are derived from (crawl_id, url); scanning the crawl's
        # own rows avoids trusting JSON text matching.
        for item in self.items():
            if item.request_id == request_id:
                return item
        return None

    # ------------------------------------------------------------------
    # Completion / receipts
    # ------------------------------------------------------------------
    def may_execute(self, request_id: str, lease_id: str,
                    fence: Optional[int] = None, *, now: Optional[float] = None) -> bool:
        """Last-moment gate a worker must pass *before* sending the request.

        Once a lease has been reclaimed (worker timeout, crash, replacement),
        the old worker's fence is stale and this returns ``False`` even though
        its process is still alive.
        """
        return self.leases.validate(lease_id, request_id=request_id, fence=fence,
                                    now=resolve_now(self.state.clock, now))

    def complete(self, request_id: str, *, lease_id: Optional[str] = None,
                 fence: Optional[int] = None, outcome: str = "executed",
                 actual_cost: Optional[float] = None,
                 executed_at: Optional[float] = None,
                 now: Optional[float] = None) -> dict:
        """Settle a dispatched request.

        A stale or reclaimed lease still produces a *receipt* (the origin may
        indeed have been hit) but the result is flagged; the charge is never
        refunded after dispatch.
        """
        now = resolve_now(self.state.clock, now)
        item = self._by_request(request_id)
        lease_valid = False
        if lease_id is not None:
            lease_valid = self.leases.validate(lease_id, request_id=request_id,
                                               fence=fence, now=now)
        else:
            current = self.leases.current(request_id)
            if current is not None:
                lease_id = current["lease_id"]
                lease_valid = self.leases.validate(lease_id, request_id=request_id,
                                                   fence=fence, now=now)
        receipt = self.budget.commit(item.budget_key() if item is not None else request_id,
                                     actual_cost=actual_cost,
                                     outcome=outcome, executed_at=executed_at, now=now)
        if lease_id is not None:
            self.leases.release(lease_id, state="done" if outcome != "failed" else "failed", now=now)
        if item is not None:
            item.state = "executed" if outcome != "failed" else "failed"
            item.reason = receipt.reason
            item.lease_id = None
            item.fence = None
            self._save(item, now=now)
        self.state.event("request_completed", request_id=request_id, detail={
            "outcome": outcome, "lease_valid": lease_valid, "receipt": receipt.as_dict(),
        }, at=now)
        return {
            "request_id": request_id,
            "accepted": receipt.accepted,
            "state": receipt.state,
            "outcome": outcome,
            "late": receipt.late,
            "charged": receipt.charged,
            "refunded": receipt.refunded,
            "lease_valid": lease_valid,
            "lease_stale": lease_id is not None and not lease_valid,
            "receipt": receipt.as_dict(),
        }

    def fail(self, request_id: str, *, lease_id: Optional[str] = None,
             fence: Optional[int] = None, retryable: bool = True,
             reason: str = "failed", now: Optional[float] = None) -> dict:
        """Record a failure.

        The reservation is *not* refunded: the request may have reached the
        origin.  A retryable failure simply becomes eligible again; a
        non-retryable one stays failed.
        """
        now = resolve_now(self.state.clock, now)
        result = self.complete(request_id, lease_id=lease_id, fence=fence,
                               outcome="failed", now=now)
        item = self._by_request(request_id)
        if item is not None:
            if retryable:
                item.state = "discovered"
                item.reason = reason
                item.lease_id = None
                item.fence = None
                # The failed attempt keeps its charge; the retry will book a
                # separate, additive reservation under a new key.
                item.next_attempt()
            self._save(item, now=now)
        result["retryable"] = retryable
        result["retry_budget_key"] = item.budget_key() if (item is not None and retryable) else None
        return result

    def mark_unknown(self, request_id: str, *, reason: str = "outcome_unknown",
                     now: Optional[float] = None) -> bool:
        now = resolve_now(self.state.clock, now)
        item = self._by_request(request_id)
        key = item.budget_key() if item is not None else request_id
        changed = self.budget.mark_unknown(key, reason=reason, now=now)
        item = self._by_request(request_id)
        if item is not None:
            lease = self.leases.current(request_id)
            if lease is not None:
                self.leases.release(lease["lease_id"], state="unknown", now=now)
            item.state = "unknown"
            item.reason = reason
            item.lease_id = None
            item.fence = None
            self._save(item, now=now)
        return changed

    # ------------------------------------------------------------------
    # Host feedback
    # ------------------------------------------------------------------
    def report_host(self, host: str, *, retry_after: Optional[float] = None,
                    status: Optional[int] = None, now: Optional[float] = None) -> dict:
        """Apply origin feedback (429/503/Retry-After) to host pacing."""
        now = resolve_now(self.state.clock, now)
        seconds = retry_after if retry_after is not None else (
            60.0 if status in (429, 503) else 0.0)
        if seconds <= 0:
            return {"host": host, "wait_until": 0.0, "reason": None}
        state = self.politeness.impose_wait(
            host, seconds, reason=f"http_{status}" if status else "retry_after", now=now)
        self.state.event("host_feedback", detail={
            "host": host, "status": status, "retry_after": retry_after,
            "wait_until": state.wait_until,
        }, at=now)
        return {"host": host, "wait_until": state.wait_until, "reason": state.wait_reason}

    # ------------------------------------------------------------------
    # Policy change and recovery
    # ------------------------------------------------------------------
    def apply_policy(self, doc: Any, *, now: Optional[float] = None,
                     release: bool = True) -> dict:
        """Activate new rules and invalidate pending work they forbid.

        For each invalidated item the planner releases its budget reservation
        (only if still ``reserved``) and its slot (only if still pending), and
        records why.  Executed/unknown work is never refunded.
        """
        now = resolve_now(self.state.clock, now)
        previous = self.policy.active
        previous_version = previous.version if previous is not None else None
        new_rules = self.policy.register(doc, now=now)
        pending = []
        executing = []
        for item in self.items():
            if item.state in self.PENDING_STATES:
                pending.append({
                    "url": item.url, "request_id": item.request_id, "depth": item.depth,
                    "robots": item.robots, "state": "pending",
                    "policy_version": item.policy_version,
                    "user_agent": self.user_agent,
                })
            elif item.state in ("leased", "in_flight", "unknown", "executed", "failed"):
                executing.append(item)
        invalid = self.policy.invalidate(pending, rules=new_rules, user_agent=self.user_agent)
        # Executing or already-settled work may still be affected by the new
        # rules, but it is not cancelled and never refunded.  Report it so the
        # operator can see *why* quota did not come back.
        not_refunded = []
        for item in executing:
            decision = self.policy.check(item.url, user_agent=self.user_agent,
                                         robots=item.robots, depth=item.depth,
                                         now=now, rules=new_rules)
            ledger = None
            try:
                ledger = self.budget.get(item.budget_key())
            except Exception:
                ledger = None
            not_refunded.append({
                "request_id": item.request_id,
                "url": item.url,
                "state": item.state,
                "newly_denied": not decision.allowed,
                "decision_reason": decision.reason,
                "ledger_state": ledger["state"] if ledger is not None else None,
                "reason": "executed_or_unknown_not_refundable",
            })
        self.policy.activate(new_rules.doc, now=now)
        released = []
        if release:
            for entry in invalid:
                request_id = entry.get("request_id")
                if not request_id:
                    continue
                item = self._by_request(request_id)
                reservation = self.budget.release(
                    item.budget_key() if item is not None else request_id,
                    reason="policy_invalidated", now=now)
                queue_item = self.politeness.dequeue(request_id)
                if item is not None:
                    item.state = "invalidated"
                    item.reason = entry["reason"]
                    item.policy_version = new_rules.version
                    self._save(item, now=now)
                released.append({"request_id": request_id, "url": entry["url"],
                                 "reason": entry["reason"], "refunded": reservation.refunded,
                                 "released": reservation.released,
                                 "dequeued": queue_item is not None})
        self.state.event("policy_invalidation", detail={
            "version": new_rules.version, "invalidated": len(invalid), "released": len(released),
            "not_refunded": len(not_refunded),
        }, at=now)
        return {
            "version": new_rules.version,
            "previous": previous_version,
            "invalidated": invalid,
            "released": released,
            "not_refunded": not_refunded,
        }

    def recover(self, *, now: Optional[float] = None) -> dict:
        """Repair state after a crash / clock anomaly.

        * expired leases are reclaimed (slots freed);
        * requests that were in flight and lost their lease become ``unknown``
          and stay charged;
        * expired pending reservations are refunded;
        * the clock high-water mark is persisted so days do not reopen.

        ``now`` is optional; when omitted the logical clock is used.  A written
        clock that jumped backwards is clamped by the persisted high-water mark.
        """
        now = resolve_now(self.state.clock, now)
        reclaimed = self.leases.reclaim(now=now)
        unknown = []
        for entry in reclaimed:
            reclaimed_item = self._by_request(entry["request_id"])
            key = reclaimed_item.budget_key() if reclaimed_item is not None else entry["request_id"]
            self.budget.mark_unknown(key, reason="lease_reclaimed", now=now)
            item = reclaimed_item
            if item is not None and item.state in ("leased", "in_flight"):
                item.state = "unknown"
                item.reason = "lease_reclaimed"
                item.lease_id = None
                item.fence = None
                self._save(item, now=now)
            unknown.append(entry["request_id"])
        expired = self.budget.expire(now=now)
        self.state.set_meta("clock_high_water", self.state.clock.high_water)
        result = {
            "at": now,
            "clock_high_water": self.state.clock.high_water,
            "reclaimed_leases": reclaimed,
            "unknown_requests": unknown,
            "refunded_reservations": expired["released"],
            "expired_in_flight": expired["unknown"],
        }
        self.state.event("recovery", detail=result, at=now)
        return result

    # ------------------------------------------------------------------
    # Batch execution driver
    # ------------------------------------------------------------------
    def run_batch(self, fetcher: Callable[[str], Any], *, limit: Optional[int] = None,
                  worker: str = "worker", now: Optional[float] = None,
                  outcome_for: Optional[Callable[[str, Any], str]] = None,
                  retryable_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
                  max_passes: int = 1000, ttl: Optional[float] = None) -> dict:
        """Dispatch and execute work until no further progress is possible.

        ``fetcher`` returns ``(status, body)`` / ``(status, body, headers)`` /
        an object with ``status``.  The driver:

        * re-plans between passes, so a released slot or elapsed pacing window
          lets the next request through in the same call;
        * dispatches in ranked order (host pacing and quota are enforced by
          :meth:`dispatch`; nothing is bypassed here);
        * re-validates the lease immediately before sending, so a worker whose
          lease was reclaimed does not execute;
        * feeds 429/503 + ``Retry-After`` back into host pacing;
        * settles each request exactly once.

        It stops as soon as a pass makes no progress (for example every
        remaining URL is waiting on a host wait or the daily quota).  The
        return value contains executed/failed/deferred details plus the fresh
        plan, so progress and every deferral reason are visible in one object.
        """
        now = resolve_now(self.state.clock, now)
        executed: list[dict] = []
        failed: list[dict] = []
        deferred: dict[str, dict] = {}
        # A request that failed in this run is not retried in the same run:
        # retries are paced by host waits and the next planning cycle, which
        # prevents a hot loop against a persistently failing origin.
        attempted: dict[str, dict] = {}
        passes = 0
        while passes < max_passes:
            passes += 1
            plan = self.plan(now=now, limit=limit, reserve=False)
            for entry in plan["deferrals"]:
                deferred.setdefault(entry["request_id"], {
                    "request_id": entry["request_id"], "url": entry["url"],
                    "reason": entry["reason"], "retry_at": entry.get("retry_at"),
                })
            batch = [record for record in plan["batch"]
                     if record["request_id"] not in attempted]
            if not batch:
                break
            progress = False
            for record in batch:
                request_id = record["request_id"]
                attempted[request_id] = {"request_id": request_id,
                                          "url": str(record.get("url", ""))}
                deferred.pop(request_id, None)
                dispatched = self.dispatch(request_id, worker=worker, now=now, ttl=ttl)
                if not dispatched.granted:
                    deferred[request_id] = {
                        "request_id": request_id, "url": dispatched.url,
                        "reason": dispatched.reason, "retry_at": dispatched.retry_at,
                    }
                    continue
                lease = dispatched.lease
                if lease is None or not self.may_execute(request_id, lease.lease_id,
                                                         fence=lease.fence, now=now):
                    deferred[request_id] = {"request_id": request_id,
                                            "url": dispatched.url,
                                            "reason": "lease_stale"}
                    continue
                try:
                    response = fetcher(dispatched.url)
                except Exception as exc:
                    self.fail(request_id, lease_id=lease.lease_id, fence=lease.fence,
                              retryable=True, reason=f"fetch_error:{type(exc).__name__}",
                              now=now)
                    failed.append({"request_id": request_id, "url": dispatched.url,
                                   "reason": f"fetch_error:{type(exc).__name__}",
                                   "error": str(exc)})
                    progress = True
                    continue
                status, body = _coerce_page(response)
                if status in (429, 503):
                    self.report_host(dispatched_host(dispatched.url),
                                     retry_after=_retry_after(response), status=status, now=now)
                if status in retryable_statuses:
                    self.fail(request_id, lease_id=lease.lease_id, fence=lease.fence,
                              retryable=True, reason=f"http_{status}", now=now)
                    failed.append({"request_id": request_id, "url": dispatched.url,
                                   "status": status, "reason": f"http_{status}",
                                   "retry_at": self.politeness.next_allowed(
                                       dispatched_host(dispatched.url))})
                    progress = True
                    continue
                outcome = outcome_for(dispatched.url, response) if outcome_for else (
                    "executed" if status < 400 else "failed")
                self.complete(request_id, lease_id=lease.lease_id, fence=lease.fence,
                              outcome=outcome, now=now)
                if outcome == "executed":
                    executed.append({"request_id": request_id, "url": dispatched.url,
                                     "status": status, "bytes": len(body or "")})
                else:
                    failed.append({"request_id": request_id, "url": dispatched.url,
                                   "status": status, "reason": "http_error"})
                progress = True
            if not progress:
                break
        return {
            "at": now,
            "passes": passes,
            "executed": executed,
            "failed": failed,
            "deferred": list(deferred.values()),
            "counts": {"executed": len(executed), "failed": len(failed),
                       "deferred": len(deferred)},
            "plan": self.plan(now=now),
        }

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def explain(self, request_id: str) -> dict:
        """Full audit trail for one request: evidence, budget, lease, events."""
        item = self._by_request(request_id)
        key = item.budget_key() if item is not None else request_id
        ledger = self.budget.get(key)
        events = [e for e in self.state.events(limit=10_000) if e.get("request_id") == request_id]
        return {
            "request_id": request_id,
            "item": item.as_dict() if item else None,
            "ledger": None if ledger is None else {**dict(ledger),
                                                   "payload": json.loads(ledger["payload"] or "{}")},
            "attempts": self.leases.attempts(request_id),
            "events": events,
            "policy_version": self.policy.active.version if self.policy.active else None,
        }

    def report(self, *, now: Optional[float] = None) -> dict:
        """Coverage, pace, quota and deferral summary for the whole crawl."""
        now = resolve_now(self.state.clock, now)
        plan = self.plan(now=now)
        discovery = self.state.get_json("discovery", self.crawl_id, {}) or {}
        budget = self.budget.summary(now=now)
        return {
            "crawl_id": self.crawl_id,
            "report_at": now,
            "coverage": {
                **plan["coverage"],
                "discovery": discovery,
                "denial_reasons": _count_by(plan["buckets"]["denied"], "reason"),
            },
            "pace": plan["pace"],
            "budget": budget,
            "deferrals": plan["deferrals"],
            "counts": plan["counts"],
            "policy": self.policy.active.as_dict() if self.policy.active else None,
            "policy_versions": self.policy.versions(),
            "robots_versions": self._robots_version_summary(),
        }

    def _robots_version_summary(self) -> list[dict]:
        rows = self.state.query(
            "SELECT host,user_agent,version,status,fetched_at,expires_at FROM robots_current cur "
            "JOIN robots_cache c USING(host,user_agent,version) ORDER BY host")
        return [dict(r) for r in rows]

    def release_slot(self, request_id: str, *, now: Optional[float] = None,
                     reason: str = "slot_released") -> bool:
        """Free a worker slot for a request whose outcome is not yet known.

        Freeing the slot does **not** free the quota: the request was already
        dispatched, so it becomes ``unknown`` (charged) until a receipt says
        otherwise.
        """
        now = resolve_now(self.state.clock, now)
        lease = self.leases.current(request_id)
        if lease is None:
            return False
        self.mark_unknown(request_id, reason=reason, now=now)
        return True


def next_reset(now: float) -> float:
    from .clock import next_day_start
    return next_day_start(now)


def _coerce_page(response: Any) -> tuple[int, Any]:
    if response is None:
        return 200, ""
    if isinstance(response, tuple):
        status = int(response[0])
        body = response[1] if len(response) > 1 else ""
        return status, body
    if hasattr(response, "status"):
        return int(response.status), getattr(response, "text", "")
    return 200, response


def _retry_after(response: Any) -> Optional[float]:
    headers = None
    if isinstance(response, tuple) and len(response) > 2 and isinstance(response[2], dict):
        headers = response[2]
    elif hasattr(response, "headers"):
        headers = getattr(response, "headers")
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dispatched_host(url: str) -> str:
    return urlmod.host_of(url)


def _ranked_record(entry) -> dict:
    """Flatten a Ranked object into the report/plan record shape."""
    return {
        "request_id": entry.candidate.tie,
        "url": entry.candidate.url,
        "host": entry.candidate.host,
        "rank": entry.rank,
        "score": round(entry.score, 6),
        "breakdown": entry.breakdown,
        "priority": entry.candidate.priority,
        "deadline": entry.candidate.deadline,
        "cost": entry.candidate.cost,
    }


def _count_by(rows: list[dict], key: str) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


__all__ = ["Planner", "Item", "DispatchResult", "request_id_for", "TERMINAL"]
