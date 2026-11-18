"""Persistent state for the crawl planner (single responsibility: storage).

The legacy ``store`` module keeps the original idempotent record table.  The
crawler needs a second, richer state store: robots evidence, policy versions,
sitemap discoveries, host pacing, reservations, leases, attempts and the clock
high-water mark all have to survive a process restart.

All writes go through one SQLite connection and short ``BEGIN IMMEDIATE``
transactions so that reservation/lease decisions are atomic.  Timestamps are
logical (see :mod:`robotscope.clock`), never trusted wall clock readings.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .clock import Clock, resolve_clock

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(scope, key)
);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    request_id TEXT,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger(
    request_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    cost REAL NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ledger_day_state ON ledger(day, state);
CREATE TABLE IF NOT EXISTS attempts(
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    fence INTEGER NOT NULL,
    worker TEXT,
    state TEXT NOT NULL,
    day TEXT NOT NULL,
    started REAL,
    expires REAL,
    ended REAL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_request ON attempts(request_id);
CREATE TABLE IF NOT EXISTS slots(
    host TEXT NOT NULL,
    request_id TEXT PRIMARY KEY,
    lease_id TEXT,
    acquired_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS slots_host ON slots(host);
CREATE TABLE IF NOT EXISTS robots_meta(
    host TEXT PRIMARY KEY,
    content TEXT,
    digest TEXT,
    etag TEXT,
    last_modified TEXT,
    fetched_at REAL,
    expires_at REAL,
    http_status INTEGER,
    status TEXT NOT NULL,
    backoff_until REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS robots_cache(
    host TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT,
    status TEXT NOT NULL,
    fetched_at REAL,
    expires_at REAL,
    backoff_until REAL,
    http_status INTEGER,
    error TEXT,
    content TEXT,
    rules TEXT,
    created_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(host, user_agent, version)
);
CREATE TABLE IF NOT EXISTS robots_current(
    host TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY(host, user_agent)
);
CREATE TABLE IF NOT EXISTS policies(
    version TEXT PRIMARY KEY,
    doc TEXT NOT NULL,
    created_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discovered(
    crawl_id TEXT NOT NULL,
    url TEXT NOT NULL,
    payload TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY(crawl_id, url)
);
CREATE TABLE IF NOT EXISTS sitemap_cache(
    url TEXT PRIMARY KEY,
    version TEXT,
    fetched_at REAL,
    expires_at REAL,
    status TEXT NOT NULL,
    kind TEXT,
    entries TEXT,
    etag TEXT,
    last_modified TEXT,
    complete INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS hosts(
    host TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    next_allowed_at REAL,
    wait_until REAL,
    wait_reason TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS crawl_urls(
    crawl_id TEXT NOT NULL,
    url TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    host TEXT,
    policy_version TEXT,
    updated_at REAL,
    PRIMARY KEY(crawl_id, url)
);
"""


class State:
    """SQLite-backed crawl state.

    ``path`` may be ``":memory:"`` for tests or a file path for restart
    durability.  The connection is shared by the planner components; a lock
    serialises access so a worker thread cannot interleave a transaction.
    """

    def __init__(self, path: str = ":memory:", *, clock: Optional[Clock] = None) -> None:
        self.path = str(path)
        self.con = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self._in_tx = False

        high_water = float(self.get_meta("clock_high_water", 0.0) or 0.0)
        if clock is None:
            self.clock = Clock(high_water=high_water, on_advance=self._persist_high_water)
        else:
            self.clock = resolve_clock(clock)
            self.clock.observe(high_water)
            self.clock.set_on_advance(self._persist_high_water)
        self.set_meta("schema_version", SCHEMA_VERSION)
        self.set_meta("clock_high_water", self.clock.high_water)

    # -- low level -----------------------------------------------------
    def _persist_high_water(self, value: float) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO kv(scope,key,value,updated_at) VALUES('meta','clock_high_water',?,?) "
                "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (repr(float(value)), float(value)),
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._in_tx:
                yield
                return
            self.con.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            try:
                yield
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
            else:
                self.con.execute("COMMIT")
            finally:
                self._in_tx = False

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.con.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.con.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- meta / JSON documents -----------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE scope='meta' AND key=?", (key,))
        return default if row is None else row["value"]

    def set_meta(self, key: str, value: Any) -> None:
        now = self.clock.now()
        with self._lock:
            self.con.execute(
                "INSERT INTO kv(scope,key,value,updated_at) VALUES('meta',?,?,?) "
                "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, str(value), now),
            )

    def get_json(self, scope: str, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE scope=? AND key=?", (scope, key))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_json(self, scope: str, key: str, value: Any) -> None:
        now = self.clock.now()
        with self._lock:
            self.con.execute(
                "INSERT INTO kv(scope,key,value,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (scope, key, json.dumps(value, ensure_ascii=False, sort_keys=True), now),
            )

    # -- observability --------------------------------------------------
    def event(self, kind: str, *, request_id: Optional[str] = None, detail: Any = None,
              at: Optional[float] = None) -> int:
        at = self.clock.now() if at is None else float(at)
        with self._lock:
            cur = self.con.execute(
                "INSERT INTO events(at,kind,request_id,detail) VALUES(?,?,?,?)",
                (at, kind, request_id, json.dumps(detail if detail is not None else {}, ensure_ascii=False, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def events(self, limit: int = 200) -> list[dict]:
        rows = self.query(
            "SELECT seq,at,kind,request_id,detail FROM events ORDER BY seq DESC LIMIT ?", (int(limit),)
        )
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item["detail"])
            except (TypeError, ValueError):
                pass
            out.append(item)
        return list(reversed(out))

    def close(self) -> None:
        with self._lock:
            self.con.close()


__all__ = ["State", "SCHEMA_VERSION"]
