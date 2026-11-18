"""Concurrency slots and leases.

Three accounting domains are intentionally separate:

============================  =============================================
domain                        owner
============================  =============================================
daily quota (budget units)    :mod:`robotscope.budget`
host pacing / host wait       :mod:`robotscope.politeness`
concurrency slots (workers)   :this module
============================  =============================================

A request that is waiting on its host must not consume a worker slot, and a
duplicate request must not consume a second slot.  Leases carry a monotonic
``fence`` per request: once a lease is reclaimed (worker died or timed out), a
stale worker holding the old fence is refused by :meth:`LeaseManager.validate`
and by :meth:`LeaseManager.renew`, so it cannot execute after its lease was
recycled.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from .clock import resolve_now

DEFAULT_LEASE_TTL = 120.0


@dataclass
class Lease:
    lease_id: Optional[str]
    request_id: str
    host: str
    worker: Optional[str]
    fence: int
    granted: bool
    reason: str = "acquired"
    expires_at: Optional[float] = None
    duplicate: bool = False
    slot_global: bool = False
    slot_host: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class SlotPolicy:
    global_limit: Optional[int] = None
    host_limit: Optional[int] = None
    lease_ttl: float = DEFAULT_LEASE_TTL

    def to_dict(self) -> dict:
        return {"global_limit": self.global_limit, "host_limit": self.host_limit,
                "lease_ttl": self.lease_ttl}


class LeaseManager:
    """Persisted lease bookkeeping.

    ``acquire`` is atomic: it checks slot counts and inserts the lease in one
    transaction.  A second acquire for the same in-flight ``request_id``
    returns the existing lease (``duplicate=True``) without taking another
    slot.
    """

    def __init__(self, state, *, global_limit: Optional[int] = None,
                 host_limit: Optional[int] = None, lease_ttl: float = DEFAULT_LEASE_TTL) -> None:
        self.state = state
        self.policy = SlotPolicy(
            global_limit=None if global_limit is None else int(global_limit),
            host_limit=None if host_limit is None else int(host_limit),
            lease_ttl=float(lease_ttl),
        )
        self.state.set_json("slots", "policy", self.policy.to_dict())

    # -- helpers --------------------------------------------------------
    def _fence_key(self, request_id: str) -> str:
        return f"lease_fence:{request_id}"

    def _next_fence(self, request_id: str) -> int:
        current = int(self.state.get_meta(self._fence_key(request_id), 0) or 0)
        fence = current + 1
        self.state.set_meta(self._fence_key(request_id), fence)
        return fence

    def active_leases(self) -> list[dict]:
        rows = self.state.query(
            "SELECT attempt_id AS lease_id, request_id, fence, worker, state, started, expires, payload "
            "FROM attempts WHERE state='active' ORDER BY started, attempt_id")
        return [dict(r) for r in rows]

    def current(self, request_id: str) -> Optional[dict]:
        row = self.state.one(
            "SELECT attempt_id AS lease_id, request_id, fence, worker, state, started, expires "
            "FROM attempts WHERE request_id=? AND state='active' ORDER BY fence DESC LIMIT 1",
            (request_id,))
        return dict(row) if row else None

    def slot_usage(self, host: Optional[str] = None) -> dict:
        rows = self.state.query("SELECT host, COUNT(*) AS n FROM slots GROUP BY host")
        by_host = {r["host"]: int(r["n"]) for r in rows}
        return {
            "global": sum(by_host.values()),
            "by_host": by_host,
            "host": None if host is None else by_host.get(host, 0),
            "global_limit": self.policy.global_limit,
            "host_limit": self.policy.host_limit,
        }

    # -- acquire / renew / release -------------------------------------
    def acquire(self, request_id: str, host: str, *, worker: Optional[str] = None,
                now: Optional[float] = None, ttl: Optional[float] = None) -> Lease:
        request_id = str(request_id)
        host = str(host)
        now = resolve_now(self.state.clock, now)
        ttl = self.policy.lease_ttl if ttl is None else float(ttl)
        with self.state.transaction():
            existing = self.current(request_id)
            if existing is not None and existing["expires"] is not None and existing["expires"] > now:
                return Lease(lease_id=existing["lease_id"], request_id=request_id, host=host,
                             worker=existing["worker"], fence=int(existing["fence"]), granted=True,
                             reason="duplicate_lease", expires_at=existing["expires"], duplicate=True,
                             slot_global=True, slot_host=True)
            # A stale lease row for this request (expired but not yet
            # reclaimed) is retired before a new fence is issued.
            if existing is not None:
                self._retire(existing["lease_id"], "superseded", now)
            usage = self.slot_usage(host)
            if self.policy.global_limit is not None and usage["global"] >= self.policy.global_limit:
                self.state.event("lease_denied", request_id=request_id,
                                 detail={"host": host, "reason": "global_slots_full",
                                         "usage": usage}, at=now)
                return Lease(None, request_id, host, worker, 0, False, "global_slots_full")
            if self.policy.host_limit is not None and usage["host"] >= self.policy.host_limit:
                self.state.event("lease_denied", request_id=request_id,
                                 detail={"host": host, "reason": "host_slots_full", "usage": usage}, at=now)
                return Lease(None, request_id, host, worker, 0, False, "host_slots_full")
            fence = self._next_fence(request_id)
            lease_id = f"L-{uuid.uuid4().hex[:12]}"
            expires = now + ttl
            self.state.execute(
                "INSERT INTO attempts(attempt_id,request_id,fence,worker,state,day,started,expires,ended,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (lease_id, request_id, fence, worker, "active", "", now, expires, None, "{}"),
            )
            self.state.execute(
                "INSERT INTO slots(host,request_id,lease_id,acquired_at) VALUES(?,?,?,?) "
                "ON CONFLICT(request_id) DO UPDATE SET lease_id=excluded.lease_id,"
                "host=excluded.host,acquired_at=excluded.acquired_at",
                (host, request_id, lease_id, now),
            )
            self.state.event("lease_acquired", request_id=request_id, detail={
                "lease_id": lease_id, "host": host, "fence": fence, "worker": worker,
                "expires_at": expires,
            }, at=now)
            return Lease(lease_id, request_id, host, worker, fence, True, "acquired",
                         expires_at=expires, slot_global=True, slot_host=True)

    def _retire(self, lease_id: str, reason: str, now: float, *, ended_state: str = "expired") -> None:
        row = self.state.one("SELECT * FROM attempts WHERE attempt_id=?", (lease_id,))
        if row is None:
            return
        self.state.execute(
            "UPDATE attempts SET state=?, ended=? WHERE attempt_id=?", (ended_state, now, lease_id))
        self.state.execute("DELETE FROM slots WHERE lease_id=?", (lease_id,))
        self.state.event("lease_retired", request_id=row["request_id"], detail={
            "lease_id": lease_id, "fence": row["fence"], "reason": reason,
        }, at=now)

    def renew(self, lease_id: str, *, fence: Optional[int] = None,
              now: Optional[float] = None, ttl: Optional[float] = None) -> bool:
        """Extend a live lease.

        Returns ``False`` when the lease was reclaimed or superseded (the
        caller's fence is no longer current) — a stale worker must stop.
        """
        now = resolve_now(self.state.clock, now)
        ttl = self.policy.lease_ttl if ttl is None else float(ttl)
        with self.state.transaction():
            row = self.state.one("SELECT * FROM attempts WHERE attempt_id=?", (lease_id,))
            if row is None or row["state"] != "active":
                return False
            current_fence = int(self.state.get_meta(self._fence_key(row["request_id"]), 0) or 0)
            if fence is not None and int(fence) != int(row["fence"]):
                return False
            if int(row["fence"]) != current_fence:
                return False
            if row["expires"] is not None and now >= row["expires"]:
                return False
            self.state.execute("UPDATE attempts SET expires=? WHERE attempt_id=?", (now + ttl, lease_id))
            return True

    def validate(self, lease_id: str, *, request_id: Optional[str] = None,
                 fence: Optional[int] = None, now: Optional[float] = None) -> bool:
        """True only if *this exact* lease may still execute work."""
        now = resolve_now(self.state.clock, now)
        row = self.state.one("SELECT * FROM attempts WHERE attempt_id=?", (lease_id,))
        if row is None or row["state"] != "active":
            return False
        if request_id is not None and row["request_id"] != request_id:
            return False
        if fence is not None and int(row["fence"]) != int(fence):
            return False
        if row["expires"] is not None and now >= row["expires"]:
            return False
        current_fence = int(self.state.get_meta(self._fence_key(row["request_id"]), 0) or 0)
        return int(row["fence"]) == current_fence

    def release(self, lease_id: str, *, state: str = "done", now: Optional[float] = None) -> bool:
        """Release a lease and its slot.  Idempotent."""
        now = resolve_now(self.state.clock, now)
        with self.state.transaction():
            row = self.state.one("SELECT * FROM attempts WHERE attempt_id=?", (lease_id,))
            if row is None or row["state"] != "active":
                return False
            self._retire(lease_id, "released", now, ended_state=state)
            return True

    def reclaim(self, *, now: Optional[float] = None) -> list[dict]:
        """Reclaim every expired lease.  Returns the reclaimed attempts.

        Callers must decide the budget consequence (an expired in-flight
        request is *unknown*, not free); the lease layer itself only frees
        slots.
        """
        now = resolve_now(self.state.clock, now)
        reclaimed = []
        rows = self.state.query(
            "SELECT * FROM attempts WHERE state='active' AND expires IS NOT NULL AND expires <= ?", (now,))
        for row in rows:
            host = self.state.one("SELECT host FROM slots WHERE request_id=?", (row["request_id"],))
            with self.state.transaction():
                self._retire(row["attempt_id"], "expired", now)
            reclaimed.append({
                "lease_id": row["attempt_id"], "request_id": row["request_id"],
                "host": host["host"] if host is not None else None,
                "fence": row["fence"], "worker": row["worker"],
            })
        return reclaimed

    def attempts(self, request_id: Optional[str] = None) -> list[dict]:
        if request_id is None:
            rows = self.state.query("SELECT * FROM attempts ORDER BY started, attempt_id")
        else:
            rows = self.state.query(
                "SELECT * FROM attempts WHERE request_id=? ORDER BY started, attempt_id", (request_id,))
        return [dict(r) for r in rows]

    def metric(self) -> dict:
        return {"policy": self.policy.to_dict(), "usage": self.slot_usage(),
                "active": self.active_leases()}


__all__ = ["Lease", "SlotPolicy", "LeaseManager", "DEFAULT_LEASE_TTL"]
