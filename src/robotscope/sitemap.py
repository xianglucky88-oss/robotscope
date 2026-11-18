"""Sitemap discovery: recursive index handling, merging and completeness.

Key invariants
--------------
* Sitemap indexes are walked recursively.  Cycles (A indexes B indexes A) and
  shared references (two indexes pointing at the same child) are visited once;
  the parent link is still recorded as provenance.
* One failing child does not abort the scan.  The failure is recorded, the
  remaining children are still fetched, and the aggregate result is marked
  **incomplete**.
* An incomplete scan is never evidence that a URL disappeared: callers can ask
  :func:`deletion_candidates` and get an empty list unless the scan is
  complete.
* Each fetched sitemap carries a content version so a re-parse is
  reproducible and cache hits are visible in the report.
"""
from __future__ import annotations

import gzip
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
from xml.etree import ElementTree as ET

from . import urls as urlmod
from .clock import resolve_now

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_DEPTH = 12
MAX_SITEMAPS = 5000
MAX_URLS = 500_000
DEFAULT_TTL = 6 * 3600.0


@dataclass
class Entry:
    url: str
    lastmod: Optional[str] = None
    changefreq: Optional[str] = None
    priority: Optional[float] = None
    sources: tuple[str, ...] = ()

    def merge(self, other: "Entry") -> "Entry":
        return Entry(
            url=self.url,
            lastmod=_max_lastmod(self.lastmod, other.lastmod),
            changefreq=self.changefreq or other.changefreq,
            priority=_max_priority(self.priority, other.priority),
            sources=tuple(sorted(set(self.sources) | set(other.sources))),
        )

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "lastmod": self.lastmod,
            "changefreq": self.changefreq,
            "priority": self.priority,
            "sources": list(self.sources),
        }


@dataclass
class SitemapDoc:
    url: str
    version: Optional[str]
    kind: str                     # "urlset" | "index" | "text" | "unknown"
    entries: list[Entry] = field(default_factory=list)
    child_urls: list[str] = field(default_factory=list)
    status: str = "ok"            # "ok" | "error" | "cached"
    error: Optional[str] = None
    http_status: Optional[int] = None
    depth: int = 0
    parents: list[str] = field(default_factory=list)
    complete: bool = True
    ancestry: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "version": self.version,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "http_status": self.http_status,
            "depth": self.depth,
            "parents": list(self.parents),
            "entries": len(self.entries),
            "children": len(self.child_urls),
            "complete": self.complete,
        }


@dataclass
class Discovery:
    """Aggregate result of walking one or more sitemap roots."""

    roots: list[str]
    entries: dict[str, Entry] = field(default_factory=dict)
    documents: dict[str, SitemapDoc] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    cycle_edges: list[tuple[str, str]] = field(default_factory=list)
    shared_edges: list[tuple[str, str]] = field(default_factory=list)
    complete: bool = True
    truncated: bool = False

    def urls(self) -> list[str]:
        return sorted(self.entries)

    def as_dict(self) -> dict:
        return {
            "roots": list(self.roots),
            "complete": self.complete,
            "truncated": self.truncated,
            "entries": len(self.entries),
            "documents": len(self.documents),
            "failures": list(self.failures),
            "cycle_edges": [list(e) for e in self.cycle_edges],
            "shared_edges": [list(e) for e in self.shared_edges],
            "detail": [doc.as_dict() for doc in self.documents.values()],
        }


def _max_lastmod(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)  # ISO-8601 strings sort lexicographically when well formed


def _max_priority(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(text: str, *, base_url: Optional[str] = None, source: Optional[str] = None) -> tuple[str, list[Entry], list[str]]:
    """Parse a sitemap document.

    Returns ``(kind, entries, child_urls)``.  Sitemap index files return child
    URLs; urlset/text files return entries.  Malformed XML degrades to a text
    scan so a partially broken document still yields what it can.
    """
    source = source or base_url or ""
    raw = text or ""
    kind = "unknown"
    entries: list[Entry] = []
    children: list[str] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return _parse_text(raw, source=source)

    tag = _localname(root.tag)
    if tag == "sitemapindex":
        kind = "index"
        for node in root:
            if _localname(node.tag) != "sitemap":
                continue
            loc = _child_text(node, "loc")
            if loc:
                try:
                    children.append(urlmod.normalize(loc, base=source))
                except urlmod.URLError:
                    continue
    elif tag == "urlset":
        kind = "urlset"
        for node in root:
            if _localname(node.tag) != "url":
                continue
            loc = _child_text(node, "loc")
            if not loc:
                continue
            try:
                normalized = urlmod.normalize(loc, base=source)
            except urlmod.URLError:
                continue
            priority = None
            prio_text = _child_text(node, "priority")
            if prio_text:
                try:
                    priority = float(prio_text)
                except ValueError:
                    priority = None
            entries.append(Entry(
                url=normalized,
                lastmod=_child_text(node, "lastmod"),
                changefreq=_child_text(node, "changefreq"),
                priority=priority,
                sources=(source,),
            ))
    else:
        return _parse_text(raw, source=source)
    return kind, entries, children


def _child_text(node, name: str) -> Optional[str]:
    for child in node:
        if _localname(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def _parse_text(raw: str, *, source: str) -> tuple[str, list[Entry], list[str]]:
    entries: list[Entry] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("<"):
            continue
        try:
            normalized = urlmod.normalize(line, base=source)
        except urlmod.URLError:
            continue
        entries.append(Entry(url=normalized, sources=(source,)))
    return ("text" if entries else "unknown"), entries, []


def _decode(payload: Any) -> str:
    if isinstance(payload, bytes):
        if payload[:2] == b"\x1f\x8b":
            try:
                payload = gzip.decompress(payload)
            except OSError:
                pass
        return payload.decode("utf-8", errors="replace")
    return str(payload or "")


class SitemapWalker:
    """Walk sitemap roots with a shared cache.

    ``fetcher`` receives a URL and may return a string, bytes or
    ``(status, text)``; raising an exception is recorded as a local failure.
    """

    def __init__(self, state=None, *, fetcher: Optional[Callable[..., Any]] = None,
                 ttl: float = DEFAULT_TTL, max_depth: int = MAX_DEPTH,
                 max_sitemaps: int = MAX_SITEMAPS, max_urls: int = MAX_URLS) -> None:
        self.state = state
        self.fetcher = fetcher
        self.ttl = float(ttl)
        self.max_depth = int(max_depth)
        self.max_sitemaps = int(max_sitemaps)
        self.max_urls = int(max_urls)

    # -- cache ---------------------------------------------------------
    def _read_cache(self, url: str, now: float):
        if self.state is None:
            return None
        row = self.state.one("SELECT * FROM sitemap_cache WHERE url=?", (url,))
        if row is None or row["status"] != "ok":
            return None
        if row["expires_at"] is not None and now >= row["expires_at"]:
            return None
        return row

    def _write_cache(self, url: str, *, now: float, version: Optional[str], kind: str,
                     entries: list[Entry], children: list[str], status: str = "ok",
                     error: Optional[str] = None, http_status: Optional[int] = None) -> None:
        if self.state is None:
            return
        import json
        with self.state.transaction():
            self.state.execute(
                "INSERT INTO sitemap_cache(url,version,fetched_at,expires_at,status,kind,entries,etag,"
                "last_modified,complete,error) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET version=excluded.version,fetched_at=excluded.fetched_at,"
                "expires_at=excluded.expires_at,status=excluded.status,kind=excluded.kind,"
                "entries=excluded.entries,complete=excluded.complete,error=excluded.error",
                (url, version, now, now + self.ttl if status == "ok" else now, status, kind,
                 json.dumps({"entries": [e.as_dict() for e in entries], "children": children},
                            ensure_ascii=False),
                 None, None, 1 if status == "ok" else 0, error),
            )
        self.state.event("sitemap_fetched", detail={
            "url": url, "version": version, "kind": kind, "status": status,
            "entries": len(entries), "children": len(children), "error": error,
            "http_status": http_status,
        }, at=now)

    def _fetch(self, url: str, now: float) -> tuple[str, str, int]:
        """Return ``(kind, text, http_status)`` or raise on failure."""
        if self.fetcher is None:
            raise RuntimeError("no sitemap fetcher configured")
        result = self.fetcher(url)
        if isinstance(result, tuple):
            status = int(result[0])
            text = _decode(result[1] if len(result) > 1 else "")
        elif hasattr(result, "status") and hasattr(result, "text"):
            status = int(result.status)
            text = _decode(result.text)
        else:
            status = 200
            text = _decode(result)
        if status in (404, 410):
            raise FileNotFoundError(f"HTTP {status}")
        if status < 200 or status >= 300:
            raise RuntimeError(f"HTTP {status}")
        return text, status

    # -- traversal -----------------------------------------------------
    def walk(self, roots: Iterable[str], *, base: Optional[str] = None,
             now: Optional[float] = None) -> Discovery:
        now = resolve_now(self.state.clock if self.state is not None else None, now)
        normalized_roots: list[str] = []
        for root in roots:
            if not root:
                continue
            try:
                normalized_roots.append(urlmod.normalize(root, base=base))
            except urlmod.URLError:
                continue
        discovery = Discovery(roots=normalized_roots)
        seen: set[str] = set()
        # Stack items: (url, depth, parent, ancestry tuple)
        stack: list[tuple[str, int, Optional[str], tuple[str, ...]]] = []
        for root in normalized_roots:
            stack.append((root, 0, None, (root,)))
        # Depth-first but stable order: process in reverse so the first root is
        # visited first.
        while stack:
            url, depth, parent, ancestry = stack.pop()
            if len(discovery.documents) >= self.max_sitemaps:
                discovery.truncated = True
                discovery.complete = False
                self._failure(discovery, url, parent, "sitemap_limit_reached", now)
                break
            if url in seen:
                # Already visited: either a shared reference (two indexes
                # linking the same child) or a cycle (the child is an ancestor
                # of the parent).  Both are safe to skip but mean different
                # things and are reported separately.
                doc = discovery.documents.get(url)
                parent_doc = discovery.documents.get(parent) if parent else None
                is_cycle = bool(
                    parent is not None
                    and (parent == url or (parent_doc is not None and url in parent_doc.ancestry))
                )
                if parent:
                    if is_cycle:
                        discovery.cycle_edges.append((parent, url))
                    else:
                        discovery.shared_edges.append((parent, url))
                    if doc is not None and parent not in doc.parents:
                        doc.parents.append(parent)
                continue
            seen.add(url)
            inherited = tuple(discovery.documents[parent].ancestry) if parent in discovery.documents else ()
            doc = SitemapDoc(url=url, version=None, kind="unknown", depth=depth,
                             parents=[parent] if parent else [])
            doc.ancestry = inherited + (url,)
            discovery.documents[url] = doc
            if depth > self.max_depth:
                doc.status = "error"
                doc.error = "max_depth_exceeded"
                doc.complete = False
                discovery.complete = False
                self._failure(discovery, url, parent, "max_depth_exceeded", now)
                continue
            cached = self._read_cache(url, now)
            if cached is not None:
                import json
                payload = json.loads(cached["entries"] or "{}")
                doc.version = cached["version"]
                doc.kind = cached["kind"] or "unknown"
                doc.status = "cached"
                doc.complete = bool(cached["complete"])
                try:
                    doc.entries = [Entry(**e) for e in payload.get("entries", [])]
                    doc.child_urls = [u for u in payload.get("children", []) if isinstance(u, str)]
                except Exception:
                    doc.entries, doc.child_urls = [], []
                if not doc.complete:
                    discovery.complete = False
                self._merge_entries(discovery, doc, url)
                children = list(doc.child_urls)
            else:
                try:
                    text, http_status = self._fetch(url, now)
                except Exception as exc:
                    doc.status = "error"
                    doc.error = f"{type(exc).__name__}: {exc}"
                    doc.complete = False
                    discovery.complete = False
                    self._write_cache(url, now=now, version=None, kind="unknown", entries=[],
                                      children=[], status="error", error=doc.error)
                    self._failure(discovery, url, parent, doc.error, now)
                    continue
                kind, entries, children = parse_sitemap(text, base_url=url, source=url)
                version = "s:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                doc.version = version
                doc.kind = kind
                doc.entries = entries
                doc.child_urls = children
                doc.http_status = http_status
                doc.status = "ok"
                self._write_cache(url, now=now, version=version, kind=kind, entries=entries,
                                  children=children, http_status=http_status)
                self._merge_entries(discovery, doc, url)
            if len(discovery.entries) > self.max_urls:
                discovery.truncated = True
                discovery.complete = False
                break
            # Push children (reverse for deterministic order).
            for child in reversed(children):
                if child in ancestry:
                    discovery.cycle_edges.append((url, child))
                    continue
                stack.append((child, depth + 1, url, ancestry + (child,)))
        return discovery

    def _merge_entries(self, discovery: Discovery, doc: SitemapDoc, source: str) -> None:
        for entry in doc.entries:
            entry.sources = tuple(sorted(set(entry.sources) | {source}))
            existing = discovery.entries.get(entry.url)
            discovery.entries[entry.url] = entry if existing is None else existing.merge(entry)

    def _failure(self, discovery: Discovery, url: str, parent: Optional[str], error: str, now: float) -> None:
        discovery.failures.append({"url": url, "parent": parent, "error": error, "at": now})


def walk(roots: Iterable[str], *, fetcher: Optional[Callable[..., Any]] = None,
         state=None, base: Optional[str] = None, now: Optional[float] = None) -> Discovery:
    return SitemapWalker(state=state, fetcher=fetcher).walk(roots, base=base, now=now)


def deletion_candidates(discovery: Discovery, known_urls: Iterable[str]) -> list[str]:
    """URLs previously known but absent from this scan.

    Returns an empty list unless the scan is complete, because an unfinished
    scan cannot prove that anything was deleted.
    """
    if not discovery.complete or discovery.truncated:
        return []
    seen = set(discovery.entries)
    return sorted(set(known_urls) - seen)


# ---------------------------------------------------------------------------
# Legacy pipeline stage
# ---------------------------------------------------------------------------
def process(record):
    """Legacy single-responsibility stage.

    Copies a structured ``sitemap`` payload (if present) into ``sitemap_urls``
    without making crawl decisions.  Keeps the historical stage marker.
    """
    rec = dict(record)
    rec.setdefault("_stage_sitemap", "ok")
    payload = rec.get("sitemap")
    if isinstance(payload, dict):
        rec["sitemap_urls"] = sorted(set(payload.get("urls") or []))
        rec["sitemap_complete"] = bool(payload.get("complete", False))
    elif isinstance(payload, list):
        rec["sitemap_urls"] = sorted({str(u) for u in payload})
        rec["sitemap_complete"] = False
    return rec


__all__ = [
    "Entry",
    "SitemapDoc",
    "Discovery",
    "SitemapWalker",
    "parse_sitemap",
    "walk",
    "deletion_candidates",
    "process",
]
