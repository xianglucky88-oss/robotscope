"""URL identity, scope and sitemap/robots path helpers.

The crawler must be able to say *why* two spellings are the same URL and to
derive a stable host/path that robots rules can be matched against.  This
module is the single place where that normalisation happens.
"""
from __future__ import annotations

import posixpath
import re
from typing import Iterable, Optional
from urllib.parse import (
    quote,
    unquote,
    urljoin,
    urlsplit,
    urlunsplit,
)

DEFAULT_PORTS = {"http": "80", "https": "443"}

# Characters left untouched by urllib's quoting; '%' is kept so already-encoded
# octets survive re-canonicalisation.
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = "/%:@!$&'()*+,;=-._~?"


class URLError(ValueError):
    """Raised when a URL cannot be used for crawling."""


def split(url: str):
    return urlsplit(url.strip())


def normalize(url: str, *, base: Optional[str] = None) -> str:
    """Return a canonical absolute URL.

    - scheme/host lowercased, default port removed
    - fragments dropped
    - dot segments resolved, percent escapes normalised
    - query parameters sorted (they are part of URL identity for crawling)
    - empty path becomes ``/``
    """
    if base:
        url = urljoin(base, url)
    if not isinstance(url, str):
        raise URLError(f"not a string URL: {url!r}")
    url = url.strip()
    if not url:
        raise URLError("empty URL")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise URLError(f"unsupported scheme: {scheme or '(none)'}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise URLError("URL has no host")
    port = parts.port
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    path = _normalize_path(path)
    path = quote(unquote(path), safe=_PATH_SAFE)
    query = parts.query
    if query:
        query = _normalize_query(query)
    return urlunsplit((scheme, netloc, path, query, ""))


def _normalize_path(path: str) -> str:
    trailing = path.endswith("/")
    decoded = unquote(path)
    # Resolve dot segments without collapsing a trailing slash.
    norm = posixpath.normpath(decoded)
    if not norm.startswith("/"):
        norm = "/" + norm
    if norm == "/.":
        norm = "/"
    if trailing and not norm.endswith("/"):
        norm += "/"
    return norm


def _normalize_query(query: str) -> str:
    pairs = []
    for item in query.split("&"):
        if not item:
            continue
        key, sep, value = item.partition("=")
        pairs.append((quote(unquote(key), safe=_QUERY_SAFE), sep, quote(unquote(value), safe=_QUERY_SAFE)))
    pairs.sort(key=lambda p: (p[0], p[2]))
    return "&".join(f"{k}{s}{v}" for k, s, v in pairs)


def host_of(url: str) -> str:
    """Canonical host[:port] used for robots and politeness bucketing."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    if port is not None and str(port) != DEFAULT_PORTS.get(parts.scheme.lower()):
        return f"{host}:{port}"
    return host


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    port = parts.port
    netloc = parts.hostname or ""
    if port is not None and str(port) != DEFAULT_PORTS.get(parts.scheme.lower()):
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme.lower()}://{netloc}"


def path_of(url: str, *, with_query: bool = True) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if with_query and parts.query:
        return f"{path}?{parts.query}"
    return path


def is_http(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() in ("http", "https")
    except ValueError:
        return False


def same_site(url: str, host: str) -> bool:
    """True when *url* stays on the crawled host (subdomains are distinct hosts)."""
    return host_of(url) == host


def resolve_urls(base: str, values: Iterable[str]) -> list[str]:
    out = []
    for value in values:
        if value is None:
            continue
        try:
            out.append(normalize(value, base=base))
        except URLError:
            continue
    return out


def crawl_delay_key(host: str) -> str:
    return f"host:{host}"


__all__ = [
    "URLError",
    "normalize",
    "host_of",
    "origin_of",
    "path_of",
    "is_http",
    "same_site",
    "resolve_urls",
    "split",
]
