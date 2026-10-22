"""Shared SQLite connection helpers for the many processes writing noema.db."""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

BUSY_TIMEOUT_MS = int(os.getenv("NOEMA_SQLITE_BUSY_MS", "30000"))


LOCK_LOG_S = float(os.getenv("NOEMA_SQLITE_LOCK_LOG_S", "3"))


def _log(event: str, **fields: object) -> None:
    fields.update(event=event, pid=os.getpid(), proc=os.path.basename(sys.argv[0] or "?"),
                  ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    print(json.dumps(fields, default=str), file=sys.stderr, flush=True)


class TimedConnection(sqlite3.Connection):
    """Logs write transactions that hold the noema.db lock longer than LOCK_LOG_S."""

    _txn_at: float | None = None
    _txn_sql: str = ""

    def _mark(self, sql: str) -> None:
        if self._txn_at is None and self.in_transaction:
            self._txn_at, self._txn_sql = time.monotonic(), sql.strip().split("\n")[0][:80]

    def execute(self, sql, *args):  # type: ignore[no-untyped-def, override]
        cur = super().execute(sql, *args)
        self._mark(sql)
        return cur

    def executemany(self, sql, *args):  # type: ignore[no-untyped-def, override]
        cur = super().executemany(sql, *args)
        self._mark(sql)
        return cur

    def _end(self, how: str) -> None:
        if self._txn_at is not None:
            held = time.monotonic() - self._txn_at
            if held >= LOCK_LOG_S:
                _log("sqlite_lock_held_long", held_s=round(held, 2), end=how,
                     first_sql=self._txn_sql, db=self._db_name)
        self._txn_at = None

    def commit(self) -> None:
        super().commit()
        self._end("commit")

    def rollback(self) -> None:
        super().rollback()
        self._end("rollback")


def connect(path: str | Path, *, timeout_ms: int | None = None, **kwargs) -> sqlite3.Connection:
    """Open a writer connection with WAL, a generous busy_timeout and NORMAL sync."""
    ms = BUSY_TIMEOUT_MS if timeout_ms is None else timeout_ms
    kwargs.setdefault("factory", TimedConnection)
    conn = sqlite3.connect(path, timeout=ms / 1000, **kwargs)
    if isinstance(conn, TimedConnection):
        conn._db_name = os.path.basename(str(path)).split("?")[0]
    conn.execute(f"PRAGMA busy_timeout={int(ms)}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass  # another writer holds the lock; WAL is persistent once set
    return conn


def is_busy(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg)


def retry_write[T](fn: Callable[[], T], *, attempts: int = 6, base: float = 0.25,
                conn: sqlite3.Connection | None = None) -> T:
    """Run a write, retrying with jittered exponential backoff on SQLITE_BUSY/locked."""
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_busy(exc) or i == attempts - 1:
                raise
            _log("sqlite_busy_retry", attempt=i + 1, error=str(exc)[:80])
            if conn is not None and conn.in_transaction:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            time.sleep(min(8.0, base * 2**i) * (0.5 + random.random()))
    raise AssertionError("unreachable")
