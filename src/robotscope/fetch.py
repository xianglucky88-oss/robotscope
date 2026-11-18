"""Fetchers: an offline replay fetcher and an optional HTTP fetcher.

The planner never opens a socket by itself; it receives a callable.  This keeps
tests deterministic and keeps the policy modules free of I/O.

``OfflineFetcher`` serves responses from a mapping so the whole flow can be
demonstrated from fixtures::

    {"https://example.com/robots.txt": {"status": 200, "body": "User-agent: *\\nDisallow: /x"}}

A plain string value means HTTP 200 with that body.  A missing key returns
HTTP 404 for sitemaps and raises :class:`urllib.error.URLError`-like failure for
robots (callers decide); ``strict=True`` makes every miss an error.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


class FetchError(RuntimeError):
    """Network-level failure (as opposed to an HTTP status)."""


class OfflineFetcher:
    def __init__(self, responses: Mapping[str, Any], *, strict: bool = False) -> None:
        self.responses = dict(responses)
        self.strict = bool(strict)
        self.calls: list[str] = []

    @classmethod
    def from_file(cls, path: str | Path, *, strict: bool = False) -> "OfflineFetcher":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("offline fixture must map URL -> response")
        return cls(data, strict=strict)

    def __call__(self, url: str):
        self.calls.append(url)
        if url not in self.responses:
            if self.strict:
                raise FetchError(f"no offline response for {url}")
            if url.endswith("/robots.txt"):
                return (404, "")
            raise FetchError(f"no offline response for {url}")
        value = self.responses[url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, str):
            return (200, value)
        if isinstance(value, dict):
            status = int(value.get("status", 200))
            if value.get("error"):
                raise FetchError(str(value["error"]))
            body = value.get("body", "")
            return (status, body, dict(value.get("headers") or {}))
        raise TypeError(f"unsupported offline response for {url}: {type(value)!r}")

    def robots(self, url: str):
        """Robots-friendly view: a miss means "host publishes no robots"."""
        try:
            return self(url)
        except FetchError:
            return (404, "")


def http_fetcher(user_agent: str = "robotscope", *, timeout: float = 20.0,
                 verify: bool = True, max_bytes: int = 5_000_000):
    """Return an ``httpx``-backed fetcher (imported lazily).

    The fetcher follows the project rule of never trusting a failure as
    permission: network errors raise, so the robots cache records them.
    """
    try:
        import httpx  # noqa: WPS433 (optional dependency)
    except Exception as exc:  # pragma: no cover - exercised only without httpx
        raise FetchError("httpx is not installed; use an offline fetcher") from exc

    client = httpx.Client(
        headers={"user-agent": user_agent},
        timeout=timeout,
        verify=verify,
        follow_redirects=True,
    )

    def fetch(url: str):
        try:
            response = client.get(url)
        except Exception as exc:  # network failure -> raise, never "allow"
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        body = response.text[:max_bytes]
        headers = {k.lower(): v for k, v in response.headers.items()}
        return (response.status_code, body, headers)

    fetch.client = client  # type: ignore[attr-defined]
    fetch.close = client.close  # type: ignore[attr-defined]
    return fetch


__all__ = ["FetchError", "OfflineFetcher", "http_fetcher"]
