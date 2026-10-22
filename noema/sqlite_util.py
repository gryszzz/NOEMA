"""Shared SQLite connection helpers for the many processes writing noema.db."""
from __future__ import annotations

import os
import random
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

BUSY_TIMEOUT_MS = int(os.getenv("NOEMA_SQLITE_BUSY_MS", "30000"))


def connect(path: str | Path, *, timeout_ms: int | None = None, **kwargs) -> sqlite3.Connection:
    """Open a writer connection with WAL, a generous busy_timeout and NORMAL sync."""
    ms = BUSY_TIMEOUT_MS if timeout_ms is None else timeout_ms
    conn = sqlite3.connect(path, timeout=ms / 1000, **kwargs)
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
            if conn is not None and conn.in_transaction:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            time.sleep(min(8.0, base * 2**i) * (0.5 + random.random()))
    raise AssertionError("unreachable")
