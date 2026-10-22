"""Shared polite-concurrency helpers for the read-only crawlers.

* ``HostGate``: thread-safe per-host spacing + a per-host concurrency cap (default 1), so
  different hosts are fetched in parallel while each provider still sees our old polite cadence.
* 429/503 ``Retry-After`` handling: the host is put on a cooldown (bounded) and skipped.
* ``fetch_concurrently``: run fetch callables across a bounded thread pool, keeping input order.
* ``SweepMeter`` + ``crawl_sweeps`` table: sweep duration, requests, errors, 429s, new items.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

MAX_COOLDOWN_SECONDS = 1800.0
DEFAULT_COOLDOWN_SECONDS = 60.0


def retry_after_seconds(value: str | None, default: float = DEFAULT_COOLDOWN_SECONDS) -> float:
    if not value:
        return default
    try:
        secs = float(value)
    except ValueError:
        try:
            secs = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            secs = default
    return max(1.0, min(MAX_COOLDOWN_SECONDS, secs))


class HostGate:
    def __init__(self, spacing: float, host_spacing: dict[str, float] | None = None,
                 per_host: int = 1, sleep: Callable[[float], None] = time.sleep) -> None:
        self.spacing, self.host_spacing, self.per_host, self.sleep = spacing, host_spacing or {}, per_host, sleep
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Semaphore] = {}
        self._last: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}

    def _sem(self, host: str) -> threading.Semaphore:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Semaphore(self.per_host))

    def cooling(self, host: str) -> float:
        return max(0.0, self._cooldown.get(host, 0.0) - time.monotonic())

    def cool(self, host: str, seconds: float) -> None:
        with self._lock:
            self._cooldown[host] = max(self._cooldown.get(host, 0.0), time.monotonic() + seconds)

    def acquire(self, host: str) -> threading.Semaphore:
        sem = self._sem(host)
        sem.acquire()
        need = max(self.spacing, self.host_spacing.get(host, 0.0)) if self.spacing else 0.0
        with self._lock:
            gap = time.monotonic() - self._last.get(host, -1e9)
        if gap < need:
            self.sleep(need - gap)
        with self._lock:
            self._last[host] = time.monotonic()
        return sem


def fetch_concurrently[T, R](fn: Callable[[T], R], items: Iterable[T], workers: int = 8) -> list[R]:
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items)), thread_name_prefix="crawl") as ex:
        return list(ex.map(fn, items))


SCHEMA = """CREATE TABLE IF NOT EXISTS crawl_sweeps (id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawler TEXT NOT NULL, started_at TEXT NOT NULL, duration_s REAL NOT NULL, requests INTEGER NOT NULL,
    errors INTEGER NOT NULL, rate_limited INTEGER NOT NULL, not_modified INTEGER NOT NULL,
    new_items INTEGER NOT NULL, next_sleep_s REAL)"""


class SweepMeter:
    def __init__(self, crawler: str) -> None:
        self.crawler, self.t0 = crawler, time.monotonic()
        self.started = datetime.now(UTC).isoformat()
        self.requests = self.errors = self.rate_limited = self.not_modified = 0
        self._lock = threading.Lock()

    def record(self, status: Any) -> None:
        with self._lock:
            self.requests += 1
            if status == 304:
                self.not_modified += 1
            elif status == 429:
                self.rate_limited += 1
                self.errors += 1
            elif not isinstance(status, int) or status == 0 or status >= 400:
                self.errors += 1

    def summary(self, new_items: int = 0, next_sleep: float | None = None) -> dict[str, Any]:
        return {"crawler": self.crawler, "duration_s": round(time.monotonic() - self.t0, 2),
                "requests": self.requests, "errors": self.errors, "rate_limited": self.rate_limited,
                "not_modified": self.not_modified, "new_items": new_items, "next_sleep_s": next_sleep}

    def save(self, conn: sqlite3.Connection, new_items: int = 0, next_sleep: float | None = None) -> dict[str, Any]:
        s = self.summary(new_items, next_sleep)
        try:
            conn.execute(SCHEMA)
            conn.execute("INSERT INTO crawl_sweeps (crawler, started_at, duration_s, requests, errors, rate_limited,"
                         " not_modified, new_items, next_sleep_s) VALUES (?,?,?,?,?,?,?,?,?)",
                         (self.crawler, self.started, s["duration_s"], s["requests"], s["errors"],
                          s["rate_limited"], s["not_modified"], new_items, next_sleep))
            conn.execute("DELETE FROM crawl_sweeps WHERE id NOT IN (SELECT id FROM crawl_sweeps ORDER BY id DESC "
                         "LIMIT 5000)")
            conn.commit()
        except sqlite3.Error:
            pass
        return s


def adaptive_sleep(new_items: int, base: float, *, floor: float, ceiling: float, hot: int) -> float:
    """Faster when a sweep found a lot of new things, slower when quiet."""
    if new_items >= hot:
        return floor
    if new_items == 0:
        return ceiling
    return max(floor, min(ceiling, base))


# --- live crawl-activity feed (schema tev.crawl_live.v1) --------------------------------------
LIVE_SCHEMA = """CREATE TABLE IF NOT EXISTS crawl_live (id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawler TEXT NOT NULL, source TEXT, host TEXT NOT NULL, started_at TEXT NOT NULL, duration_ms INTEGER,
    status TEXT NOT NULL, bytes INTEGER, items INTEGER)"""
LIVE_KEEP = 50


class LiveFeed:
    """Small ring table of in-flight / just-finished requests. Own short-timeout connection per write;
    failures are swallowed so telemetry can never stop a crawl."""

    def __init__(self, db_path: str | None, crawler: str) -> None:
        self.db_path, self.crawler, self._lock = db_path, crawler, threading.Lock()

    def _exec(self, sql: str, args: tuple) -> int | None:
        if not self.db_path:
            return None
        with self._lock:
            try:
                c = sqlite3.connect(self.db_path, timeout=2)
                try:
                    c.execute(LIVE_SCHEMA)
                    cur = c.execute(sql, args)
                    rid = cur.lastrowid
                    c.execute("DELETE FROM crawl_live WHERE id <= (SELECT MAX(id) FROM crawl_live) - ?", (LIVE_KEEP,))
                    c.commit()
                    return rid
                finally:
                    c.close()
            except sqlite3.Error:
                return None

    def start(self, host: str, source: str | None = None) -> tuple[int | None, float]:
        rid = self._exec("INSERT INTO crawl_live (crawler, source, host, started_at, status) VALUES (?,?,?,?,?)",
                         (self.crawler, source, host, datetime.now(UTC).isoformat(), "in_flight"))
        return rid, time.monotonic()

    def finish(self, token: tuple[int | None, float], status: Any, nbytes: int | None = None) -> None:
        rid, t0 = token
        if rid is not None:
            self._exec("UPDATE crawl_live SET status=?, duration_ms=?, bytes=? WHERE id=?",
                       (str(status), int((time.monotonic() - t0) * 1000), nbytes, rid))

    def items(self, source: str, n: int) -> None:
        self._exec("UPDATE crawl_live SET items=? WHERE id=(SELECT MAX(id) FROM crawl_live WHERE crawler=? "
                   "AND source=?)", (int(n), self.crawler, source))


def crawl_live(db_path: str, limit: int = LIVE_KEEP) -> dict[str, Any]:
    """Read-only payload for GET /api/crawl-live."""
    out: dict[str, Any] = {"schema": "tev.crawl_live.v1", "at": datetime.now(UTC).isoformat(),
                           "in_flight": 0, "entries": [], "sweeps": []}
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return out
    try:
        cols = ("id", "crawler", "source", "host", "started_at", "duration_ms", "status", "bytes", "items")
        out["entries"] = [dict(zip(cols, r, strict=True)) for r in c.execute(
            f"SELECT {', '.join(cols)} FROM crawl_live ORDER BY id DESC LIMIT ?", (min(limit, LIVE_KEEP),))]
        out["in_flight"] = sum(e["status"] == "in_flight" for e in out["entries"])
        sc = ("crawler", "started_at", "duration_s", "requests", "errors", "rate_limited", "not_modified",
              "new_items", "next_sleep_s")
        out["sweeps"] = [dict(zip(sc, r, strict=True)) for r in c.execute(
            f"SELECT {', '.join(sc)} FROM crawl_sweeps ORDER BY id DESC LIMIT 10")]
    except sqlite3.Error:
        pass
    finally:
        c.close()
    return out
