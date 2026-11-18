"""Planner policy for robots caching and batch scheduling.

The constants live here (not in the planner) so operators can tune waits
without touching coordination logic:

``ROBOTS_REFRESH_MODE``
    ``"conservative"`` (default) — a URL is only dispatched when fresh robots
    evidence exists.  A missing/expired/failed cache defers the URL.
    ``"best_effort"`` — an expired cache may still *deny* (safe) but never
    allows; it only changes when the planner refreshes, not the verdict.
``CONSERVATIVE_WAIT``
    When the only obstacle is missing evidence, the planner prefers waiting
    over a speculative request.  There is no code path that turns "unknown"
    into "allow".
"""
from __future__ import annotations

CONSERVATIVE_WAIT = True
ROBOTS_REFRESH_MODE = "conservative"

# Human-readable trade-off statement surfaced in reports.
TRADEOFF = (
    "Conservative wait over progress: missing, expired or failed robots evidence "
    "defers the URL instead of crawling it.  Host waits and quota exhaustion are "
    "reported separately so the reason for every deferral is explicit."
)


def describe() -> dict:
    return {
        "conservative_wait": CONSERVATIVE_WAIT,
        "robots_refresh_mode": ROBOTS_REFRESH_MODE,
        "tradeoff": TRADEOFF,
    }


__all__ = ["CONSERVATIVE_WAIT", "ROBOTS_REFRESH_MODE", "TRADEOFF", "describe"]
