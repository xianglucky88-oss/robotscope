"""Value ordering and starvation control.

The score combines four signals and never outranks an admission decision:

===================  ===========================================
signal               meaning
===================  ===========================================
historical yield     past success at this host/path prefix
cost                 expected request cost (budget units)
deadline             urgency relative to an optional per-item deadline
starvation           time-in-queue bonus, then round-robin fairness
===================  ===========================================

``priority`` may be a static numeric boost (site priority).  It is only used
for *ordering*; :mod:`robotscope.policy_engine` applies it after robots has
already said no, and never reverses a denial.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import urls as urlmod
from .clock import parse_time, resolve_now

DEFAULT_WEIGHTS = {
    "history": 1.0,
    "cost": 0.5,
    "deadline": 2.0,
    "starvation": 1.0,
    "priority": 1.0,
}


@dataclass
class Candidate:
    url: str
    priority: float = 0.0
    historical_score: float = 0.0
    cost: float = 1.0
    deadline: Optional[float] = None
    enqueued_at: float = 0.0
    last_served_at: Optional[float] = None
    host: Optional[str] = None
    tie: str = ""
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Ranked:
    candidate: Candidate
    score: float
    breakdown: dict
    rank: int = 0

    def as_dict(self) -> dict:
        return {
            "url": self.candidate.url,
            "host": self.candidate.host,
            "score": round(self.score, 6),
            "rank": self.rank,
            "breakdown": self.breakdown,
            "priority": self.candidate.priority,
            "deadline": self.candidate.deadline,
        }


def _history_term(value: float) -> float:
    # Log-dampened so one very productive prefix does not starve everything.
    value = max(0.0, float(value))
    return math.log1p(value)


def _cost_term(cost: float) -> float:
    cost = max(0.001, float(cost))
    return 1.0 / cost


def _deadline_term(deadline: Optional[float], now: float) -> tuple[float, Optional[float]]:
    if deadline is None:
        return 0.0, None
    remaining = float(deadline) - now
    if remaining <= 0:
        return 2.0, remaining   # already late: maximal urgency, clamped
    return min(2.0, 1.0 / max(remaining / 3600.0, 0.01)), remaining


def score(candidate: Candidate, *, now: float, weights: Optional[dict] = None,
          starvation_window: float = 600.0) -> Ranked:
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    hist = _history_term(candidate.historical_score)
    cost = _cost_term(candidate.cost)
    deadline, remaining = _deadline_term(candidate.deadline, now)
    waited = max(0.0, now - float(candidate.enqueued_at))
    starve = min(2.0, waited / max(1.0, starvation_window))
    prescore = (
        weights["history"] * hist
        + weights["cost"] * cost
        + weights["deadline"] * deadline
        + weights["starvation"] * starve
        + weights["priority"] * float(candidate.priority)
    )
    breakdown = {
        "history": round(weights["history"] * hist, 6),
        "cost": round(weights["cost"] * cost, 6),
        "deadline": round(weights["deadline"] * deadline, 6),
        "starvation": round(weights["starvation"] * starve, 6),
        "priority": round(weights["priority"] * float(candidate.priority), 6),
        "waited": round(waited, 3),
        "deadline_in": None if remaining is None else round(remaining, 3),
    }
    return Ranked(candidate=candidate, score=prescore, breakdown=breakdown)


def rank(candidates: Iterable[Candidate], *, now: Optional[float] = None,
         weights: Optional[dict] = None, starvation_window: float = 600.0,
         fairness: bool = True, round_size: int = 50) -> list[Ranked]:
    """Order candidates by score with anti-starvation.

    When ``fairness`` is enabled, the queue is processed in rounds of
    ``round_size``: within a round candidates are ordered by score, then a
    host that has not been served for the longest time gets its best candidate
    promoted to the front of the next round.  The result is a deterministic
    order (ties broken by URL) so tests and restarts reproduce it.
    """
    now = resolve_now(None, now)
    scored = [score(c, now=now, weights=weights, starvation_window=starvation_window)
              for c in candidates]
    scored.sort(key=lambda r: (-r.score, r.candidate.tie or r.candidate.url))

    if not fairness or len(scored) <= 2:
        for i, item in enumerate(scored, start=1):
            item.rank = i
        return scored

    # Rotate hosts: after every round, the least-recently-served host's head
    # moves up.  This keeps a large host from monopolising the queue forever.
    ordered: list[Ranked] = []
    last_served: dict[str, float] = {}
    for item in scored:
        host = item.candidate.host or ""
        served = item.candidate.last_served_at
        if served is not None:
            last_served[host] = min(last_served.get(host, float("inf")), float(served))
    remaining_pool = list(scored)
    while remaining_pool:
        chunk = remaining_pool[:round_size]
        remaining_pool = remaining_pool[round_size:]
        # Within the chunk, put the most starved host's head first.
        if len({(i.candidate.host or "") for i in chunk}) > 1:
            by_host: dict[str, list[Ranked]] = {}
            for item in chunk:
                by_host.setdefault(item.candidate.host or "", []).append(item)
            starved_host = min(by_host, key=lambda h: (
                last_served.get(h, -1.0),
                h,
            ))
            head = by_host[starved_host][0]
            chunk = [head] + [i for i in chunk if i is not head]
            last_served[starved_host] = now
        ordered.extend(chunk)
    for i, item in enumerate(ordered, start=1):
        item.rank = i
    return ordered


def candidates_from(entries: Iterable[dict], *, now: Optional[float] = None,
                    default_cost: float = 1.0) -> list[Candidate]:
    """Build candidates from plain dicts (sitemap entries, legacy records)."""
    now = resolve_now(None, now)
    out = []
    for entry in entries:
        url = entry.get("url")
        if not url:
            continue
        try:
            host = urlmod.host_of(url)
        except Exception:
            host = entry.get("host")
        out.append(Candidate(
            url=url,
            priority=float(entry.get("priority", 0.0) or 0.0),
            historical_score=float(entry.get("historical_score", entry.get("history", 0.0)) or 0.0),
            cost=float(entry.get("cost", default_cost) or default_cost),
            deadline=parse_time(entry.get("deadline")),
            enqueued_at=float(entry.get("enqueued_at", now) or now),
            last_served_at=parse_time(entry.get("last_served_at")),
            host=host,
            tie=str(entry.get("url")),
            meta=entry,
        ))
    return out


# ---------------------------------------------------------------------------
# Legacy pipeline stage
# ---------------------------------------------------------------------------
def process(record):
    """Legacy pipeline stage: annotate the record with a deterministic rank key."""
    rec = dict(record)
    rec.setdefault("_stage_value_rank", "ok")
    value = float(rec.get("value", 0) or 0)
    rec["rank_score"] = value
    return rec


__all__ = [
    "Candidate",
    "Ranked",
    "DEFAULT_WEIGHTS",
    "score",
    "rank",
    "candidates_from",
    "process",
]
