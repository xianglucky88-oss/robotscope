"""Per-host politeness: spacing, crawl-delay enforcement and the host queue.

Two *different* waits are tracked, because conflating them hides capacity:

* **pacing** — the next moment this host may be contacted at all.  Derived
  from the configured ``min_interval`` and any ``Crawl-delay`` in robots.
  Requests waiting for a slot are counted as ``ready``.
* **host wait** — an externally imposed backoff (HTTP 429/503,
  ``Retry-After``, robots backoff, host unresponsive).  While a host waits,
  requests targeting it are in the ``waiting`` bucket and consume no
  concurrency slot.

The queues are persisted so a restart resumes the same pacing rather than
hammering a host whose last request was milliseconds before the crash.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

from .clock import resolve_now


@dataclass
class HostState:
    host: str
    next_allowed_at: float = 0.0
    wait_until: float = 0.0
    wait_reason: Optional[str] = None
    last_request_at: Optional[float] = None
    in_flight: int = 0
    interval: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class QueueItem:
    host: str
    request_id: str
    url: str
    enqueued_at: float
    not_before: float = 0.0
    sequence: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class Politeness:
    """Host pacing + wait queue, persisted in the crawl state.

    Parameters
    ----------
    default_interval:
        Minimum seconds between two requests to the same host when robots does
        not specify a ``Crawl-delay``.
    respect_crawl_delay:
        When true, ``max(default_interval, crawl_delay)`` is used.
    max_wait:
        Optional cap for host waits; exceeding it leaves the host waiting and
        the caller receives ``deferred`` (conservative wait over progress).
    """

    def __init__(self, state, *, default_interval: float = 0.0,
                 respect_crawl_delay: bool = True, clock=None) -> None:
        self.state = state
        self.default_interval = max(0.0, float(default_interval))
        self.respect_crawl_delay = bool(respect_crawl_delay)
        self._clock = clock
        self._seq = int(self.state.get_meta("politeness_seq", 0) or 0)

    # -- persistence ----------------------------------------------------
    def _load(self, host: str) -> HostState:
        row = self.state.one("SELECT * FROM hosts WHERE host=?", (host,))
        if row is None:
            return HostState(host=host, interval=self.default_interval)
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        return HostState(
            host=host,
            next_allowed_at=float(row["next_allowed_at"] or 0.0),
            wait_until=float(row["wait_until"] or 0.0),
            wait_reason=row["wait_reason"],
            last_request_at=payload.get("last_request_at"),
            in_flight=int(payload.get("in_flight", 0)),
            interval=float(payload.get("interval", self.default_interval)),
        )

    def _save(self, hs: HostState, *, now: float) -> None:
        with self.state.transaction():
            self.state.execute(
                "INSERT INTO hosts(host,payload,next_allowed_at,wait_until,wait_reason,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
                "payload=excluded.payload,next_allowed_at=excluded.next_allowed_at,"
                "wait_until=excluded.wait_until,wait_reason=excluded.wait_reason,updated_at=excluded.updated_at",
                (hs.host, json.dumps({"last_request_at": hs.last_request_at, "in_flight": hs.in_flight,
                                       "interval": hs.interval}, sort_keys=True),
                 hs.next_allowed_at, hs.wait_until, hs.wait_reason, now),
            )

    # -- host bookkeeping ----------------------------------------------
    def state_of(self, host: str) -> HostState:
        return self._load(host)

    def interval_for(self, host: str, crawl_delay: Optional[float] = None) -> float:
        interval = self.default_interval
        if self.respect_crawl_delay and crawl_delay is not None:
            interval = max(interval, float(crawl_delay))
        return interval

    def configure_interval(self, host: str, interval: float, *, now: Optional[float] = None) -> None:
        now = resolve_now(self._clock or self.state.clock, now)
        hs = self._load(host)
        hs.interval = max(0.0, float(interval))
        self._save(hs, now=now)

    def next_allowed(self, host: str) -> float:
        hs = self._load(host)
        return max(hs.next_allowed_at, hs.wait_until)

    def ready_at(self, host: str) -> float:
        return self.next_allowed(host)

    def is_waiting(self, host: str, *, now: Optional[float] = None) -> bool:
        now = resolve_now(self._clock or self.state.clock, now)
        hs = self._load(host)
        return hs.wait_until > now

    def note_request(self, host: str, *, now: Optional[float] = None,
                     crawl_delay: Optional[float] = None) -> HostState:
        """Record that a request was just dispatched to *host*."""
        now = resolve_now(self._clock or self.state.clock, now)
        hs = self._load(host)
        interval = self.interval_for(host, crawl_delay if crawl_delay is not None else hs.interval)
        hs.interval = interval
        hs.last_request_at = now
        hs.next_allowed_at = max(hs.next_allowed_at, now + interval)
        self._save(hs, now=now)
        return hs

    def begin(self, host: str, *, now: Optional[float] = None) -> HostState:
        now = resolve_now(self._clock or self.state.clock, now)
        hs = self._load(host)
        hs.in_flight += 1
        self._save(hs, now=now)
        return hs

    def finish(self, host: str, *, now: Optional[float] = None) -> HostState:
        now = resolve_now(self._clock or self.state.clock, now)
        hs = self._load(host)
        hs.in_flight = max(0, hs.in_flight - 1)
        self._save(hs, now=now)
        return hs

    def impose_wait(self, host: str, seconds: float, *, reason: str = "backoff",
                    now: Optional[float] = None, retry_at: Optional[float] = None) -> HostState:
        """Put *host* into an externally imposed wait.

        The wait never shortens an existing one, and never moves backwards if
        the clock jumps: the new deadline is ``max(existing, target)``.
        """
        now = resolve_now(self._clock or self.state.clock, now)
        hs = self._load(host)
        target = float(retry_at) if retry_at is not None else now + max(0.0, float(seconds))
        if target > hs.wait_until:
            hs.wait_until = target
            hs.wait_reason = reason
        elif hs.wait_reason is None:
            hs.wait_reason = reason
        self._save(hs, now=now)
        self.state.event("host_wait", detail={
            "host": host, "until": hs.wait_until, "reason": hs.wait_reason,
        }, at=now)
        return hs

    def clear_wait(self, host: str, *, now: Optional[float] = None) -> HostState:
        now = resolve_now(self._clock or self.state.clock, now)
        hs = self._load(host)
        hs.wait_until = 0.0
        hs.wait_reason = None
        self._save(hs, now=now)
        return hs

    # -- queue ----------------------------------------------------------
    def enqueue(self, host: str, request_id: str, url: str, *, now: Optional[float] = None) -> QueueItem:
        now = resolve_now(self._clock or self.state.clock, now)
        queue = self._queue()
        for item in queue:
            if item.request_id == request_id:
                return item
        self._seq += 1
        item = QueueItem(host=host, request_id=request_id, url=url, enqueued_at=now, sequence=self._seq)
        queue.append(item)
        self._write_queue(queue)
        self.state.set_meta("politeness_seq", self._seq)
        return item

    def dequeue(self, request_id: str) -> Optional[QueueItem]:
        queue = self._queue()
        kept = []
        found = None
        for item in queue:
            if item.request_id == request_id and found is None:
                found = item
            else:
                kept.append(item)
        if found is not None:
            self._write_queue(kept)
        return found

    def queue(self) -> list[QueueItem]:
        return self._queue()

    def _queue(self) -> list[QueueItem]:
        raw = self.state.get_json("politeness", "queue", [])
        items = []
        for entry in raw:
            try:
                items.append(QueueItem(**entry))
            except TypeError:
                continue
        items.sort(key=lambda i: (i.sequence, i.request_id))
        return items

    def _write_queue(self, queue: Iterable[QueueItem]) -> None:
        self.state.set_json("politeness", "queue", [i.as_dict() for i in queue])

    def metric(self, *, now: Optional[float] = None) -> dict:
        """Snapshot used by the plan report."""
        now = resolve_now(self._clock or self.state.clock, now)
        hosts = {}
        for row in self.state.query("SELECT * FROM hosts ORDER BY host"):
            hs = self._load(row["host"])
            hosts[hs.host] = {
                "interval": hs.interval,
                "next_allowed_at": hs.next_allowed_at,
                "wait_until": hs.wait_until,
                "wait_reason": hs.wait_reason,
                "waiting": hs.wait_until > now,
                "in_flight": hs.in_flight,
                "ready_in": max(0.0, hs.next_allowed_at - now),
                "pacing_ready_in": max(0.0, hs.next_allowed_at - now),
                "wait_remaining": max(0.0, hs.wait_until - now),
            }
        return {"now": now, "hosts": hosts, "queued": len(self._queue())}


# ---------------------------------------------------------------------------
# Legacy pipeline stage
# ---------------------------------------------------------------------------
def process(record):
    """Legacy pipeline stage: annotate a record with host pacing metadata.

    Records without a ``url`` keep the historical marker only.
    """
    rec = dict(record)
    rec.setdefault("_stage_politeness", "naive")
    host = rec.get("host")
    if host:
        rec["host_key"] = str(host).lower()
        rec["host_delay"] = float(rec.get("crawl_delay", 0.0) or 0.0)
    return rec


__all__ = ["HostState", "QueueItem", "Politeness", "process"]
