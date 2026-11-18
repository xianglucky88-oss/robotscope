"""Test helpers: deterministic clocks, in-memory state and fake fetchers."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from robotscope.clock import Clock  # noqa: E402
from robotscope.state import State  # noqa: E402


class FakeTime:
    def __init__(self, start: float = 0.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += float(seconds)
        return self.value

    def set(self, value: float) -> float:
        self.value = float(value)
        return self.value


def make_state(start: float = 0.0, path: str = ":memory:"):
    time = FakeTime(start)
    state = State(path, clock=Clock(wall=time))
    return state, time


class FakeFetcher:
    """Map URL → response, with call counting and programmable failures."""

    def __init__(self, routes=None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise FileNotFoundError(f"no route for {url}")
        value = self.routes[url]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(url)
        return value

    def set(self, url, value):
        self.routes[url] = value

    def expires_in(self, url):
        value = self.routes.get(url)
        return value


def urlset(*urls: str) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>")


def sitemapindex(*urls: str) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</sitemapindex>")
