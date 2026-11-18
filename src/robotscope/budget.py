"""Crawl budget ledger: daily quota, reservations and receipts.

The budget is an append-only-ish ledger keyed by ``request_id``:

* ``reserve`` is idempotent — asking twice for the same request never
  allocates twice.
* A reservation is only refundable while the request is *provably
  unexecuted* (state ``reserved``).  Once it is in flight, executed or its
  outcome is unknown the charge stays; refunding it would make the accounting
  lie about work that may already have hit the origin.
* Receipts may arrive after the accounting day rolls over.  They are
  attributed to the day the request was actually executed; the later receipt
  day is recorded as ``late`` rather than moving quota between days.
* All day boundaries come from the logical clock, so a negative wall-clock
  adjustment cannot "reset" the quota or reopen yesterday.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .clock import day_of, next_day_start, resolve_now

CHARGED_STATES = ("reserved", "in_flight", "spent", "failed", "unknown")
REFUNDABLE_STATES = ("reserved",)


@dataclass
class Reservation:
    request_id: str
    granted: bool
    day: str
    cost: float
    state: str
    reason: str = "reserved"
    duplicate: bool = False
    remaining: Optional[float] = None
    limit: Optional[float] = None
    shortfall: float = 0.0
    retry_at: Optional[float] = None
    expires_at: Optional[float] = None
    host: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "granted": self.granted,
            "day": self.day,
            "cost": self.cost,
            "state": self.state,
            "reason": self.reason,
            "duplicate": self.duplicate,
            "remaining": self.remaining,
            "limit": self.limit,
            "shortfall": self.shortfall,
            "retry_at": self.retry_at,
            "expires_at": self.expires_at,
            "host": self.host,
        }


@dataclass
class Receipt:
    request_id: str
    accepted: bool
    state: str
    day: str
    charged: float
    refunded: float = 0.0
    late: bool = False
    duplicate: bool = False
    outcome: str = "executed"
    reason: str = "settled"
    execution_day: Optional[str] = None
    receipt_day: Optional[str] = None
    overage: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ReleaseResult:
    request_id: str
    released: bool
    state: str
    refunded: float = 0.0
    reason: str = "released"

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _payload(row) -> dict:
    try:
        return json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}


class Budget:
    """Quota accounting backed by :class:`robotscope.state.State`.

    ``limits`` accepts a number (daily requests for every host) or a mapping::

        {"default": 100, "hosts": {"example.com": 10}, "per_host": 25}

    A ``None`` limit means "unbounded"; the report still shows consumption.
    """

    def __init__(self, state, *, limits=None, reservation_ttl: float = 900.0) -> None:
        self.state = state
        self.limits = limits
        self.reservation_ttl = float(reservation_ttl)
        if isinstance(limits, (int, float)):
            self.default_limit: Optional[float] = float(limits)
            self.host_limits: dict[str, float] = {}
            self.per_host_limit: Optional[float] = None
        elif isinstance(limits, dict):
            default = limits.get("default")
            self.default_limit = None if default is None else float(default)
            self.host_limits = {str(k).lower(): float(v) for k, v in (limits.get("hosts") or {}).items()}
            per_host = limits.get("per_host")
            self.per_host_limit = None if per_host is None else float(per_host)
        else:
            self.default_limit = None
            self.host_limits = {}
            self.per_host_limit = None
        self.state.set_json("budget", "config", {
            "default": self.default_limit,
            "hosts": self.host_limits,
            "per_host": self.per_host_limit,
        })

    # -- day helpers ----------------------------------------------------
    def today(self, now: Optional[float] = None) -> str:
        return day_of(resolve_now(self.state.clock, now))

    def today_start(self, now: Optional[float] = None) -> float:
        return next_day_start(resolve_now(self.state.clock, now)) - 86400.0

    def _limit_for(self, host: Optional[str]) -> Optional[float]:
        if host and host.lower() in self.host_limits:
            return self.host_limits[host.lower()]
        return self.default_limit

    # -- ledger reads ---------------------------------------------------
    def get(self, request_id: str):
        return self.state.one("SELECT * FROM ledger WHERE request_id=?", (request_id,))

    def consumed(self, day: Optional[str] = None, *, host: Optional[str] = None) -> float:
        rows = self.state.query(
            "SELECT cost,payload FROM ledger WHERE day=? AND state IN (%s)" %
            ",".join("?" for _ in CHARGED_STATES),
            (day, *CHARGED_STATES),
        ) if day else self.state.query(
            "SELECT cost,payload FROM ledger WHERE state IN (%s)" %
            ",".join("?" for _ in CHARGED_STATES),
            CHARGED_STATES,
        )
        total = 0.0
        for row in rows:
            if host is not None and _payload(row).get("host") != host:
                continue
            total += float(row["cost"] or 0.0)
        return total

    def spent(self, day: Optional[str] = None) -> float:
        return self.consumed(day)

    def reservation_count(self, day: Optional[str] = None, state: Optional[str] = None) -> int:
        if day and state:
            row = self.state.one("SELECT COUNT(*) AS n FROM ledger WHERE day=? AND state=?", (day, state))
        elif day:
            row = self.state.one("SELECT COUNT(*) AS n FROM ledger WHERE day=?", (day,))
        elif state:
            row = self.state.one("SELECT COUNT(*) AS n FROM ledger WHERE state=?", (state,))
        else:
            row = self.state.one("SELECT COUNT(*) AS n FROM ledger")
        return int(row["n"] if row else 0)

    def remaining(self, day: Optional[str] = None, *, now: Optional[float] = None,
                  host: Optional[str] = None) -> Optional[float]:
        day = day or self.today(now)
        limit = self._limit_for(host)
        if limit is None and self.per_host_limit is not None:
            limit = self.per_host_limit
        if limit is None:
            return None
        used = self.consumed(day, host=host)
        return max(0.0, limit - used)

    def summary(self, day: Optional[str] = None, *, now: Optional[float] = None) -> dict:
        now = resolve_now(self.state.clock, now)
        day = day or day_of(now)
        rows = self.state.query(
            "SELECT state, COUNT(*) AS n, COALESCE(SUM(cost),0) AS total FROM ledger "
            "WHERE day=? GROUP BY state", (day,))
        by_state = {row["state"]: {"count": int(row["n"]), "cost": float(row["total"])} for row in rows}
        limit = self.default_limit
        used = sum(v["cost"] for s, v in by_state.items() if s in CHARGED_STATES)
        return {
            "day": day,
            "limit": limit,
            "per_host_limit": self.per_host_limit,
            "used": used,
            "spent": by_state.get("spent", {}).get("cost", 0.0),
            "reserved": by_state.get("reserved", {}).get("cost", 0.0),
            "in_flight": by_state.get("in_flight", {}).get("cost", 0.0),
            "unknown": by_state.get("unknown", {}).get("cost", 0.0),
            "released": by_state.get("released", {}).get("cost", 0.0),
            "remaining": None if limit is None else max(0.0, limit - used),
            "next_reset_at": next_day_start(now),
            "next_reset_day": day_of(next_day_start(now)),
            "states": by_state,
        }

    # -- reservations ---------------------------------------------------
    def reserve(self, request_id: str, cost: float = 1.0, *, host: Optional[str] = None,
                now: Optional[float] = None, ttl: Optional[float] = None,
                meta: Optional[dict] = None) -> Reservation:
        request_id = str(request_id)
        if not request_id:
            raise ValueError("request_id is required")
        cost = float(cost)
        if cost <= 0:
            raise ValueError("cost must be positive")
        now = resolve_now(self.state.clock, now)
        day = day_of(now)
        ttl = self.reservation_ttl if ttl is None else float(ttl)
        limit = self._limit_for(host)

        with self.state.transaction():
            row = self.get(request_id)
            if row is not None and row["state"] == "released":
                # A released reservation was proven unexecuted; re-reserving
                # the same attempt key is legitimate.  Supersede the old row
                # (the release remains in the event log).
                self.state.execute("DELETE FROM ledger WHERE request_id=?", (request_id,))
                self.state.event("budget_superseded_release", request_id=request_id,
                                 detail={"reason": "re_reserved_after_release"}, at=now)
                row = None
            if row is not None:
                return self._reservation_from_row(row, duplicate=True)
            used = self.consumed(day)
            host_used = self.consumed(day, host=host) if host else 0.0
            host_limit = self._limit_for(host)
            if self.per_host_limit is not None:
                host_limit = self.per_host_limit if host_limit is None else min(host_limit, self.per_host_limit)
            if limit is not None and used + cost > limit:
                shortfall = used + cost - limit
                self.state.event("budget_denied", request_id=request_id, detail={
                    "day": day, "cost": cost, "used": used, "limit": limit, "host": host,
                    "shortfall": shortfall, "retry_at": next_day_start(now),
                }, at=now)
                return Reservation(
                    request_id=request_id, granted=False, day=day, cost=cost, state="denied",
                    reason="budget_exhausted", remaining=max(0.0, limit - used), limit=limit,
                    shortfall=shortfall, retry_at=next_day_start(now), host=host,
                )
            if host_limit is not None and host_used + cost > host_limit:
                return Reservation(
                    request_id=request_id, granted=False, day=day, cost=cost, state="denied",
                    reason="host_budget_exhausted", remaining=max(0.0, host_limit - host_used),
                    limit=host_limit, shortfall=host_used + cost - host_limit,
                    retry_at=next_day_start(now), host=host,
                )
            payload = {
                "reserved_cost": cost,
                "actual_cost": None,
                "host": host,
                "reserved_at": now,
                "expires_at": now + ttl,
                "late": False,
                "outcome": None,
                "history": [],
            }
            if meta:
                payload["meta"] = meta
            self.state.execute(
                "INSERT INTO ledger(request_id,day,cost,state,payload,updated_seq) VALUES(?,?,?,?,?,0)",
                (request_id, day, cost, "reserved", json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
            self.state.event("budget_reserved", request_id=request_id, detail={
                "day": day, "cost": cost, "host": host, "expires_at": now + ttl,
            }, at=now)
            return Reservation(
                request_id=request_id, granted=True, day=day, cost=cost, state="reserved",
                reason="reserved", duplicate=False, remaining=None if limit is None else max(0.0, limit - used - cost),
                limit=limit, expires_at=now + ttl, host=host,
            )

    def _reservation_from_row(self, row, *, duplicate: bool) -> Reservation:
        payload = _payload(row)
        state = row["state"]
        return Reservation(
            request_id=row["request_id"],
            granted=state not in ("released", "denied"),
            day=row["day"],
            cost=float(row["cost"] or 0.0),
            state=state,
            reason="duplicate" if duplicate else state,
            duplicate=duplicate,
            remaining=self.remaining(row["day"]),
            limit=self._limit_for(payload.get("host")),
            expires_at=payload.get("expires_at"),
            host=payload.get("host"),
        )

    # -- lifecycle ------------------------------------------------------
    def mark_in_flight(self, request_id: str, *, lease_id: Optional[str] = None,
                       fence: Optional[int] = None, now: Optional[float] = None) -> bool:
        now = resolve_now(self.state.clock, now)
        with self.state.transaction():
            row = self.get(request_id)
            if row is None or row["state"] not in ("reserved",):
                return False
            payload = _payload(row)
            payload.update({"in_flight_at": now, "lease_id": lease_id, "fence": fence})
            payload.setdefault("history", []).append({"at": now, "event": "in_flight"})
            self.state.execute(
                "UPDATE ledger SET state='in_flight',payload=? WHERE request_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), request_id),
            )
            self.state.event("budget_in_flight", request_id=request_id,
                             detail={"lease_id": lease_id, "fence": fence}, at=now)
            return True

    def commit(self, request_id: str, *, actual_cost: Optional[float] = None,
               outcome: str = "executed", executed_at: Optional[float] = None,
               now: Optional[float] = None, reason: str = "settled") -> Receipt:
        """Settle a request after a response (or a late receipt).

        Executed work is never refunded.  Duplicate receipts do not charge
        twice.  A receipt for an already-released reservation re-charges it
        because the release assumption ("not executed") was wrong.
        """
        request_id = str(request_id)
        now = resolve_now(self.state.clock, now)
        receipt_day = day_of(now)
        with self.state.transaction():
            row = self.get(request_id)
            if row is None:
                cost = float(actual_cost if actual_cost is not None else 1.0)
                day = day_of(executed_at) if executed_at is not None else receipt_day
                payload = {"reserved_cost": None, "actual_cost": cost, "host": None,
                           "unreserved": True, "outcome": outcome, "late": False, "history": []}
                self.state.execute(
                    "INSERT INTO ledger(request_id,day,cost,state,payload) VALUES(?,?,?,?,?)",
                    (request_id, day, cost, "spent" if outcome != "failed" else "failed",
                     json.dumps(payload, sort_keys=True)),
                )
                self.state.event("budget_receipt_unreserved", request_id=request_id,
                                 detail={"cost": cost, "outcome": outcome}, at=now)
                return Receipt(request_id=request_id, accepted=True, state="spent", day=day,
                               charged=cost, outcome=outcome, reason="unreserved_receipt",
                               execution_day=day, receipt_day=receipt_day)
            payload = _payload(row)
            state = row["state"]
            if state in ("spent", "failed"):
                return Receipt(request_id=request_id, accepted=False, state=state, day=row["day"],
                               charged=float(row["cost"] or 0.0), duplicate=True,
                               outcome=payload.get("outcome") or "executed", reason="duplicate_receipt",
                               execution_day=payload.get("executed_day", row["day"]),
                               receipt_day=receipt_day)
            reservation_day = row["day"]
            reserved_cost = float(payload.get("reserved_cost") or row["cost"] or 0.0)
            requested_cost = float(actual_cost if actual_cost is not None else reserved_cost)
            if requested_cost < 0:
                raise ValueError("actual_cost must be non-negative")

            if state == "released":
                # We released it believing it never ran; the receipt proves
                # otherwise.  Re-charge, never leave executed work free.
                execution_day = day_of(executed_at) if executed_at is not None else reservation_day
                if execution_day < reservation_day:
                    execution_day = reservation_day
                payload.update({"actual_cost": requested_cost, "outcome": outcome,
                                "late": receipt_day != execution_day, "re_released": True,
                                "executed_day": execution_day, "receipt_day": receipt_day})
                payload.setdefault("history", []).append({"at": now, "event": "receipt_after_release"})
                self.state.execute(
                    "UPDATE ledger SET day=?,cost=?,state=?,payload=? WHERE request_id=?",
                    (execution_day, requested_cost,
                     "spent" if outcome != "failed" else "failed",
                     json.dumps(payload, ensure_ascii=False, sort_keys=True), request_id),
                )
                self.state.event("budget_recharged_after_release", request_id=request_id,
                                 detail={"cost": requested_cost, "day": execution_day}, at=now)
                return Receipt(request_id=request_id, accepted=True,
                               state="spent" if outcome != "failed" else "failed",
                               day=execution_day, charged=requested_cost,
                               late=receipt_day != execution_day,
                               outcome=outcome, reason="receipt_after_release",
                               execution_day=execution_day, receipt_day=receipt_day)

            # reserved / in_flight / unknown -> settle.
            executed_day = payload.get("executed_day")
            if executed_at is not None:
                executed_day = day_of(executed_at)
            execution_day = executed_day or reservation_day
            if execution_day < reservation_day:
                # Timestamp went backwards relative to the reservation: keep
                # the ledger on the reservation day and flag the anomaly.
                execution_day = reservation_day
                payload.setdefault("history", []).append({"at": now, "event": "day_rollback"})
            late = receipt_day != execution_day
            refunded = max(0.0, reserved_cost - requested_cost)
            overage = 0.0
            charge = requested_cost
            limit = self._limit_for(payload.get("host"))
            if requested_cost > reserved_cost and limit is not None:
                used_without = self.consumed(execution_day) - float(row["cost"] or 0.0)
                headroom = max(0.0, limit - used_without - reserved_cost)
                extra = min(requested_cost - reserved_cost, headroom)
                overage = (requested_cost - reserved_cost) - extra
                charge = reserved_cost + extra
            payload.update({
                "actual_cost": charge,
                "requested_cost": requested_cost,
                "overage": overage,
                "outcome": outcome,
                "late": late,
                "executed_day": execution_day,
                "receipt_day": receipt_day,
                "settled_at": now,
            })
            payload.setdefault("history", []).append(
                {"at": now, "event": "settled", "late": late, "outcome": outcome})
            new_state = "spent" if outcome != "failed" else "failed"
            self.state.execute(
                "UPDATE ledger SET day=?,cost=?,state=?,payload=? WHERE request_id=?",
                (execution_day, charge, new_state,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), request_id),
            )
            if late:
                self.state.event("budget_late_receipt", request_id=request_id, detail={
                    "execution_day": execution_day, "receipt_day": receipt_day,
                    "cost": charge, "refunded": refunded, "outcome": outcome,
                }, at=now)
            self.state.event("budget_settled", request_id=request_id, detail={
                "day": execution_day, "cost": charge, "refunded": refunded,
                "overage": overage, "outcome": outcome,
            }, at=now)
            return Receipt(request_id=request_id, accepted=True, state=new_state,
                           day=execution_day, charged=charge, refunded=refunded, late=late,
                           outcome=outcome, reason=reason, execution_day=execution_day,
                           receipt_day=receipt_day, overage=overage)

    def mark_unknown(self, request_id: str, *, reason: str = "outcome_unknown",
                     now: Optional[float] = None) -> bool:
        now = resolve_now(self.state.clock, now)
        with self.state.transaction():
            row = self.get(request_id)
            if row is None or row["state"] not in ("reserved", "in_flight"):
                return False
            payload = _payload(row)
            payload.update({"unknown_reason": reason, "unknown_at": now})
            payload.setdefault("history", []).append({"at": now, "event": "unknown", "reason": reason})
            self.state.execute(
                "UPDATE ledger SET state='unknown',payload=? WHERE request_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), request_id),
            )
            self.state.event("budget_marked_unknown", request_id=request_id,
                             detail={"reason": reason}, at=now)
            return True

    def release(self, request_id: str, *, reason: str = "unexecuted",
                now: Optional[float] = None) -> ReleaseResult:
        """Refund a reservation that is provably unexecuted.

        ``in_flight``, ``unknown``, ``spent`` and ``failed`` are *not*
        refundable; callers get ``released=False`` with the retaining reason.
        """
        request_id = str(request_id)
        now = resolve_now(self.state.clock, now)
        with self.state.transaction():
            row = self.get(request_id)
            if row is None:
                return ReleaseResult(request_id, False, "missing", 0.0, "unknown_request")
            state = row["state"]
            if state == "released":
                return ReleaseResult(request_id, False, state, 0.0, "duplicate_release")
            if state not in REFUNDABLE_STATES:
                self.state.event("budget_release_refused", request_id=request_id,
                                 detail={"state": state, "reason": reason}, at=now)
                return ReleaseResult(request_id, False, state, 0.0,
                                     "not_refundable_after_dispatch")
            refunded = float(row["cost"] or 0.0)
            payload = _payload(row)
            payload.update({"release_reason": reason, "released_at": now})
            payload.setdefault("history", []).append({"at": now, "event": "released", "reason": reason})
            self.state.execute(
                "UPDATE ledger SET state='released',cost=0,payload=? WHERE request_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), request_id),
            )
            self.state.event("budget_released", request_id=request_id,
                             detail={"reason": reason, "refunded": refunded}, at=now)
            return ReleaseResult(request_id, True, "released", refunded, reason)

    def discard(self, request_id: str, *, now: Optional[float] = None) -> bool:
        """Forget a *provably unexecuted* reservation so the same attempt key
        can be re-reserved (e.g. the slot was unavailable).

        Only rows still in ``reserved`` are deleted; the release event remains
        in the audit log.
        """
        request_id = str(request_id)
        now = resolve_now(self.state.clock, now)
        with self.state.transaction():
            row = self.get(request_id)
            if row is None or row["state"] != "reserved":
                return False
            self.state.execute("DELETE FROM ledger WHERE request_id=?", (request_id,))
            self.state.event("budget_discarded", request_id=request_id,
                             detail={"reason": "reservation_never_executed"}, at=now)
            return True

    def expire(self, *, now: Optional[float] = None) -> dict:
        """Expire reservation leases.

        A waiting reservation is refunded; an in-flight reservation is marked
        unknown (charged), because the request may have reached the origin.
        """
        now = resolve_now(self.state.clock, now)
        released, unknown = [], []
        rows = self.state.query("SELECT * FROM ledger WHERE state IN ('reserved','in_flight')")
        for row in rows:
            payload = _payload(row)
            expires = payload.get("expires_at")
            if expires is None or now < float(expires):
                continue
            if row["state"] == "reserved":
                result = self.release(row["request_id"], reason="reservation_expired", now=now)
                if result.released:
                    released.append(row["request_id"])
            else:
                if self.mark_unknown(row["request_id"], reason="in_flight_expired", now=now):
                    unknown.append(row["request_id"])
        return {"released": released, "unknown": unknown, "at": now}

    def day_rows(self, day: str) -> list[dict]:
        rows = self.state.query("SELECT * FROM ledger WHERE day=? ORDER BY request_id", (day,))
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = _payload(row)
            out.append(item)
        return out


# ---------------------------------------------------------------------------
# Legacy pipeline stage
# ---------------------------------------------------------------------------
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_budget", "naive")
    # Legacy records may carry a pre-computed allowance; never invent one.
    if "budget_limit" in rec:
        rec["budget_within_limit"] = int(rec.get("value", 0)) <= int(rec["budget_limit"])
    return rec


__all__ = [
    "Budget",
    "Reservation",
    "Receipt",
    "ReleaseResult",
    "CHARGED_STATES",
    "REFUNDABLE_STATES",
    "process",
]
