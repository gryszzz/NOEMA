"""Rate-limit-safe 1m candle + trade source for the TIMING agent (read-only public APIs, paper only).

GeckoTerminal: token -> top pool, pool -> minute OHLCV and recent trades. A token bucket keeps us
under ~25 calls/min (public limit 30). Results are cached in data/timing.db so backtests replay
stored paths and the live loop never refetches the same minute.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.request
from typing import Any

from .sqlite_util import connect as sqlite_connect

BASE = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "NOEMA-timing/1.0 (paper research)", "Accept": "application/json"}


class Bucket:
    def __init__(self, per_min: float = 25.0) -> None:
        self.gap = 60.0 / per_min
        self.next = 0.0
        self.lock = threading.Lock()

    def take(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = self.next - now
            self.next = max(now, self.next) + self.gap
        if wait > 0:
            time.sleep(wait)


BUCKET = Bucket(float(os.getenv("NOEMA_TIMING_GECKO_PER_MIN", "10")))  # crawler keeps the rest of the ~30/min


def _get(url: str, tries: int = 3) -> Any:
    for i in range(tries):
        BUCKET.take()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(5 * (i + 1) if e.code == 429 else 1)
        except (OSError, ValueError):
            time.sleep(1)
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (mint TEXT PRIMARY KEY, pool TEXT, reserve_usd REAL, fetched_at REAL);
CREATE TABLE IF NOT EXISTS candles (pool TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
  PRIMARY KEY (pool, ts));
"""


def cache(path: str) -> sqlite3.Connection:
    c = sqlite_connect(path)
    c.executescript(SCHEMA)
    return c


def top_pool(conn: sqlite3.Connection, mint: str, network: str = "solana") -> tuple[str | None, float]:
    r = conn.execute("SELECT pool, reserve_usd FROM pools WHERE mint=?", (mint,)).fetchone()
    if r:
        return r[0], r[1] or 0.0
    d = _get(f"{BASE}/networks/{network}/tokens/{mint}/pools?page=1")
    pool, res = None, 0.0
    for p in (d or {}).get("data") or []:
        a = p.get("attributes") or {}
        rv = float(a.get("reserve_in_usd") or 0)
        if rv >= res:
            pool, res = a.get("address"), rv
    conn.execute("INSERT OR REPLACE INTO pools VALUES (?,?,?,?)", (mint, pool, res, time.time()))
    conn.commit()
    return pool, res


def minute_candles(conn: sqlite3.Connection, pool: str, before_ts: int, limit: int = 300,
                   network: str = "solana", agg: int = 1) -> list[tuple]:
    d = _get(f"{BASE}/networks/{network}/pools/{pool}/ohlcv/minute?aggregate={agg}"
             f"&before_timestamp={before_ts}&limit={limit}&currency=usd")
    rows = (((d or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = [(pool, int(r[0]), *map(float, r[1:6])) for r in rows if len(r) >= 6]
    conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)", out)
    conn.commit()
    return load(conn, pool, 0, before_ts)


def load(conn: sqlite3.Connection, pool: str, t0: int, t1: int) -> list[tuple]:
    return conn.execute("SELECT ts,o,h,l,c,v FROM candles WHERE pool=? AND ts>=? AND ts<=? ORDER BY ts",
                        (pool, t0, t1)).fetchall()


def recent_trades(pool: str, network: str = "solana") -> list[dict[str, Any]]:
    d = _get(f"{BASE}/networks/{network}/pools/{pool}/trades")
    out = []
    for t in (d or {}).get("data") or []:
        a = t.get("attributes") or {}
        out.append({"side": a.get("kind"), "usd": float(a.get("volume_in_usd") or 0),
                    "at": a.get("block_timestamp")})
    return out
