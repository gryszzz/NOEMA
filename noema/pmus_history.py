"""Polymarket US quote + settlement history (READ ONLY, public endpoints, no keys) for future backtests.

Every run: page through active markets (list price per side), take a top-of-book BBO for markets closing
within BBO_HORIZON_H (capped), and fetch the settlement for every recorded market whose endDate passed.
Tables (data/pmus_history.db): pmus_markets, pmus_quotes, pmus_settlements.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .sqlite_util import connect as sqlite_connect

BBO_HORIZON_H = 48
BBO_CAP = int(os.getenv("NOEMA_PMUS_BBO_CAP", "20"))
# The public PM-US API is rate limited per IP (~5 requests / 15 s observed) and shared with the agent,
# pm_watch, xv_match and fast_settle. Pace every call and stop a phase at the first rate-limit hit
# instead of hammering through it (which starved every other PM-US reader on the box).
PACE_S = float(os.getenv("NOEMA_PMUS_PACE_S", "6.0"))


def _rate_limited(exc: BaseException) -> bool:
    return type(exc).__name__ == "RateLimitError" or "429" in str(exc)[:200]
SCHEMA = """
CREATE TABLE IF NOT EXISTS pmus_markets (slug TEXT PRIMARY KEY, question TEXT, category TEXT, market_type TEXT,
  start_date TEXT, end_date TEXT, first_seen TEXT, last_seen TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS pmus_quotes (slug TEXT, at TEXT, price REAL, best_bid REAL, best_ask REAL,
  bid_depth REAL, ask_depth REAL, last_trade REAL, source TEXT, PRIMARY KEY (slug, at, source));
CREATE INDEX IF NOT EXISTS pmus_quotes_slug ON pmus_quotes(slug, at);
CREATE TABLE IF NOT EXISTS pmus_settlements (slug TEXT PRIMARY KEY, settlement REAL, fetched_at TEXT);
"""


def connect(path: str) -> sqlite3.Connection:
    c = sqlite_connect(path)
    c.executescript(SCHEMA)
    return c


def _f(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def yes_price(m: dict[str, Any]) -> float | None:
    for s in m.get("marketSides") or []:
        if s.get("long") or str(s.get("description", "")).lower() == "yes":
            return _f(s.get("price"))
    return None


def run(db: str, client: Any = None, now: datetime | None = None) -> dict[str, int]:
    if client is None:
        from polymarket_us import PolymarketUS
        client = PolymarketUS(timeout=10.0, max_retries=1)
    now = now or datetime.now(UTC)
    iso = now.isoformat()
    c = connect(db)
    n_m = n_q = n_b = n_s = 0
    soon: list[str] = []
    off = 0
    for _ in range(1000):
        try:
            page = client.markets.list({"limit": 100, "offset": off, "closed": False, "active": True})
        except Exception:  # noqa: BLE001
            break
        ms = page.get("markets") or []
        for m in ms:
            slug = m.get("slug")
            if not slug:
                continue
            c.execute("INSERT INTO pmus_markets VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET last_seen=excluded.last_seen,"
                      " end_date=excluded.end_date", (slug, m.get("question"), m.get("category"), m.get("marketType"),
                      m.get("startDate"), m.get("endDate"), iso, iso, (m.get("description") or "")[:2000]))
            n_m += 1
            p = yes_price(m)
            if p is not None:
                c.execute("INSERT OR IGNORE INTO pmus_quotes VALUES (?,?,?,?,?,?,?,?,?)", (
                    slug, iso, p, _f(m.get("bestBidQuote")), _f(m.get("bestAskQuote")), None, None, None, "list"))
                n_q += 1
            try:
                end = datetime.fromisoformat(m.get("endDate") or "")
            except ValueError:
                end = None
            if end and now <= end <= now + timedelta(hours=BBO_HORIZON_H):
                soon.append(slug)
        c.commit()  # one short txn per page: never hold pmus_history.db across the next HTTP page
        if len(ms) < 100:
            break
        off += 100
        time.sleep(PACE_S)
    c.commit()
    for slug in soon[:BBO_CAP]:
        try:
            d = (client.markets.bbo(slug) or {}).get("marketData") or {}
        except Exception as exc:  # noqa: BLE001 - a missing quote/settlement stays unknown
            if _rate_limited(exc):
                break
            continue
        c.execute("INSERT OR IGNORE INTO pmus_quotes VALUES (?,?,?,?,?,?,?,?,?)", (
            slug, iso, _f(d.get("currentPx")), _f(d.get("bestBid")), _f(d.get("bestAsk")), _f(d.get("bidDepth")),
            _f(d.get("askDepth")), _f(d.get("lastTradePx")), "bbo"))
        n_b += 1
        c.commit()  # commit before sleeping / the next HTTP call
        time.sleep(PACE_S)
    c.commit()
    due = [r[0] for r in c.execute("SELECT slug FROM pmus_markets WHERE end_date < ? AND slug NOT IN "
                                   "(SELECT slug FROM pmus_settlements) LIMIT 60", (iso,))]
    for slug in due:
        try:
            st = client.markets.settlement(slug) or {}
        except Exception as exc:  # noqa: BLE001 - a missing quote/settlement stays unknown
            if _rate_limited(exc):
                break
            continue
        if st.get("settlement") is not None:
            c.execute("INSERT OR REPLACE INTO pmus_settlements VALUES (?,?,?)", (slug, _f(st["settlement"]), iso))
            n_s += 1
            c.commit()
        time.sleep(PACE_S)
    c.commit()
    c.close()
    return {"markets": n_m, "list_quotes": n_q, "bbo_quotes": n_b, "settlements": n_s}


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/pmus_history.db")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=1800)
    a = ap.parse_args()
    while True:
        try:
            print(datetime.now(UTC).isoformat(), json.dumps(run(a.db)), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:200]}), flush=True)
        if not a.loop:
            return
        time.sleep(a.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
