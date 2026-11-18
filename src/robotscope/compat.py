"""Compatibility: load old inputs into the new planner.

The baseline fixture and old pipeline only know flat records.  This module
normalises those shapes (and the newer structured crawl input) into a
:class:`CrawlRequest` without requiring callers to rewrite their data.

Accepted input
--------------
1. A list of flat records (legacy fixture)::

       [{"id": "r-001", "src": "alpha", "name": "...", "value": 10}]

   Legacy records carry no URL, so they are *not* crawlable; they are passed
   through as ``legacy`` items and never receive an invented permission.

2. A mapping (structured crawl input)::

       {
         "crawl_id": "example",
         "user_agent": "robotscope",
         "sitemap": ["https://example.com/sitemap.xml"],
         "urls": ["https://example.com/a"],
         "rules": {...},                     # optional policy document
         "limits": 100,                      # optional daily quota
         "priority": {"https://example.com/a": 1.0},
         "deadlines": {"https://example.com/a": "2026-12-01T00:00:00Z"},
         "history": {"example.com": 12},
         "costs": {"https://example.com/a": 2}
       }

3. A single such mapping under ``{"crawl": {...}}`` or ``{"input": ...}``
   (older wrapper styles).

Unknown keys are preserved in ``CrawlRequest.extra`` so nothing is silently
lost.  Old records keep idempotent keys via :func:`robotscope.keyer.key_of`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import urls as urlmod
from .clock import parse_time


@dataclass
class CrawlRequest:
    crawl_id: str = "crawl"
    user_agent: str = "robotscope"
    sitemaps: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    rules: Optional[dict] = None
    limits: Any = None
    min_interval: float = 0.0
    global_slots: Optional[int] = None
    host_slots: Optional[int] = None
    lease_ttl: float = 120.0
    priority: dict[str, float] = field(default_factory=dict)
    deadlines: dict[str, float] = field(default_factory=dict)
    history: dict[str, float] = field(default_factory=dict)
    costs: dict[str, float] = field(default_factory=dict)
    legacy: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def priority_for(self, url: str) -> float:
        if url in self.priority:
            return float(self.priority[url])
        host = urlmod.host_of(url)
        return float(self.priority.get(host, 0.0))

    def deadline_for(self, url: str) -> Optional[float]:
        value = self.deadlines.get(url)
        if value is None:
            value = self.deadlines.get(urlmod.host_of(url))
        return parse_time(value)

    def history_for(self, url: str) -> float:
        host = urlmod.host_of(url)
        value = self.history.get(url)
        if value is None:
            value = self.history.get(host, 0.0)
        return float(value or 0.0)

    def cost_for(self, url: str) -> float:
        value = self.costs.get(url)
        if value is None:
            value = self.costs.get(urlmod.host_of(url), 1.0)
        return float(value or 1.0)


_STRUCTURED_KEYS = {
    "crawl_id", "user_agent", "sitemap", "sitemaps", "urls", "rules", "policy",
    "limits", "budget", "min_interval", "global_slots", "host_slots", "lease_ttl",
    "priority", "deadlines", "history", "costs", "records", "seeds",
}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v]
    return []


def _float_map(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, raw in value.items():
        try:
            out[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def parse_input(data: Any) -> CrawlRequest:
    """Normalise any supported input shape into a :class:`CrawlRequest`."""
    if data is None:
        return CrawlRequest()
    if isinstance(data, list):
        request = CrawlRequest()
        urls, legacy = [], []
        for record in data:
            if not isinstance(record, dict):
                legacy.append({"value": record})
                continue
            candidate = record.get("url") or record.get("link")
            if candidate:
                try:
                    urls.append(urlmod.normalize(str(candidate)))
                    continue
                except urlmod.URLError:
                    pass
            legacy.append(record)
        request.urls = urls
        request.legacy = legacy
        return request
    if not isinstance(data, dict):
        raise TypeError("crawl input must be a mapping or a list of records")
    # Unwrap older wrappers.
    for wrapper in ("crawl", "input", "request"):
        if wrapper in data and isinstance(data[wrapper], dict) and len(data) <= 2:
            inner = parse_input(data[wrapper])
            inner.extra.setdefault("wrapper", wrapper)
            for key, value in data.items():
                if key != wrapper:
                    inner.extra.setdefault(key, value)
            return inner
    request = CrawlRequest()
    request.crawl_id = str(data.get("crawl_id") or data.get("id") or "crawl")
    request.user_agent = str(data.get("user_agent") or "robotscope")
    request.sitemaps = _as_list(data.get("sitemap", data.get("sitemaps")))
    request.urls = _as_list(data.get("urls", data.get("seeds")))
    rules = data.get("rules", data.get("policy"))
    request.rules = dict(rules) if isinstance(rules, dict) else None
    request.limits = data.get("limits", data.get("budget"))
    request.min_interval = float(data.get("min_interval") or 0.0)
    if data.get("global_slots") is not None:
        request.global_slots = int(data["global_slots"])
    if data.get("host_slots") is not None:
        request.host_slots = int(data["host_slots"])
    if data.get("lease_ttl") is not None:
        request.lease_ttl = float(data["lease_ttl"])
    request.priority = _float_map(data.get("priority"))
    request.deadlines = data.get("deadlines") if isinstance(data.get("deadlines"), dict) else {}
    request.history = _float_map(data.get("history"))
    request.costs = _float_map(data.get("costs"))
    records = data.get("records")
    if isinstance(records, list):
        request.legacy = [r for r in records if isinstance(r, dict)]
    request.extra = {k: v for k, v in data.items() if k not in _STRUCTURED_KEYS}
    return request


def plan_from_input(planner, data: Any, *, fetcher=None, robots_fetcher=None,
                    now: Optional[float] = None) -> dict:
    """Seed a planner from any supported input and return the first plan."""
    request = parse_input(data)
    if request.rules is not None:
        planner.apply_policy(request.rules, now=now, release=False)
    discovered = None
    if request.sitemaps:
        discovered = planner.discover(request.sitemaps, fetcher=fetcher, now=now)
    for url in request.urls:
        try:
            planner.add_url(url, now=now, priority=request.priority_for(url),
                            deadline=request.deadline_for(url),
                            historical_score=request.history_for(url),
                            cost=request.cost_for(url))
        except urlmod.URLError:
            continue
    planner.assess(robots_fetcher=robots_fetcher, now=now,
                   refresh_robots=robots_fetcher is not None)
    return {
        "request": request,
        "discovery": discovered,
        "plan": planner.plan(now=now),
    }


__all__ = ["CrawlRequest", "parse_input", "plan_from_input"]
