"""Logical wall clock used by the crawl planner.

A crawl can outlive a wall-clock adjustment.  Budget days, lease expiry and
"late receipt" decisions must never move backwards just because the host clock
was corrected.  ``Clock`` therefore keeps a persisted high-water mark: ``now``
is always ``max(wall_clock, high_water_mark)``.

The module is deliberately tiny; all crawl components accept either a
``Clock`` instance, a callable returning epoch seconds, or a plain number so
that tests can use a deterministic clock.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Callable, Optional


def day_of(timestamp: float) -> str:
    """Return the UTC accounting day for *timestamp* (``YYYY-MM-DD``)."""
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date().isoformat()


def next_day_start(timestamp: float) -> float:
    """Epoch seconds at which the UTC accounting day after *timestamp* starts."""
    d = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() + 86400.0


def parse_time(value, *, default: Optional[float] = None) -> Optional[float]:
    """Parse an epoch number or an ISO-8601 timestamp into epoch seconds.

    ``None``/invalid values return *default*.  Naive ISO timestamps are
    interpreted as UTC, matching the rest of the project.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return default


class Clock:
    """Monotonic wall clock with a high-water mark.

    Parameters
    ----------
    wall:
        Callable returning wall-clock epoch seconds.  Defaults to
        :func:`time.time`.
    high_water:
        Previously persisted high-water mark.  Loading it at process start is
        what keeps lease and budget decisions consistent across restarts and
        clock rollbacks.
    on_advance:
        Optional callback invoked whenever the high-water mark increases.  The
        planner wires this to persistence so an observed timestamp survives a
        restart.
    """

    def __init__(
        self,
        wall: Optional[Callable[[], float]] = None,
        high_water: float = 0.0,
        on_advance: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._wall = wall or time.time
        self._high_water = float(high_water or 0.0)
        self._on_advance = on_advance

    @property
    def high_water(self) -> float:
        return self._high_water

    def set_on_advance(self, callback: Optional[Callable[[float], None]]) -> None:
        self._on_advance = callback

    def observe(self, timestamp: float) -> float:
        """Record an externally supplied timestamp without letting time move back."""
        timestamp = float(timestamp)
        if timestamp > self._high_water:
            self._high_water = timestamp
            if self._on_advance:
                self._on_advance(timestamp)
        return self._high_water

    def now(self) -> float:
        """Return the current logical time, never lower than a prior reading."""
        try:
            wall = float(self._wall())
        except Exception:
            wall = self._high_water
        return self.observe(wall)

    def day(self, timestamp: Optional[float] = None) -> str:
        return day_of(self.now() if timestamp is None else timestamp)


def resolve_now(clock=None, now: Optional[float] = None) -> float:
    """Normalise the many clock spellings used across the project."""
    if now is not None:
        if isinstance(clock, Clock):
            return clock.observe(now)
        return float(now)
    if clock is None:
        return time.time()
    if isinstance(clock, Clock):
        return clock.now()
    if callable(clock):
        return float(clock())
    return float(clock)


def resolve_clock(clock=None) -> Clock:
    """Return a :class:`Clock` view for a callable/instance/number."""
    if isinstance(clock, Clock):
        return clock
    if clock is None:
        return Clock()
    if callable(clock):
        return Clock(wall=clock)
    value = float(clock)
    return Clock(wall=lambda: value)


__all__ = [
    "Clock",
    "day_of",
    "next_day_start",
    "parse_time",
    "resolve_clock",
    "resolve_now",
]


# Keep the import of ``date`` used for typing/readers of this module.
_ = date
