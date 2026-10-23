"""Polymarket US watched-market quote refresher (READ ONLY, public BBO, no keys, never trades).

The agent's rotating collector touches each PM-US market about once an hour, so quotes used by
edge_strategies, cross-venue pairs and news/X reaction marks were minutes old. This loop keeps a
small watched set fresh every NOEMA_PM_WATCH_SECONDS (default 30 s):

  1. links: PM-US markets linked to news / X items in the last 6 h (they get reaction marks);
  2. pairs: PM-US legs of current cross-venue pairs (xv_pairs);
  3. recent: PM-US markets in the agent's recent forecast ledger.

capped at NOEMA_PM_WATCH_CAP (default 200). One public markets.list call per 100 slugs returns
bestBidQuote/bestAskQuote for all of them (identical to per-market BBO), so a full refresh costs
1-2 requests instead of one per market. Quotes go to data/pm_watch.db (pm_watch_latest +
48 h of pm_watch_quotes history) with one short write per cycle, so noema.db is never locked.
The public websocket requires API credentials, so this uses batched REST reads.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .sqlite_util import connect as sqlite_connect

INTERVAL_S = float(os.getenv("NOEMA_PM_WATCH_SECONDS", "30"))
CAP = int(os.getenv("NOEMA_PM_WATCH_CAP", "200"))
KEEP_H = 48
DEFAULT_DB = os.getenv("NOEMA_PM_WATCH_DB", "data/pm_watch.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_watch_latest (slug TEXT PRIMARY KEY, bid REAL, ask REAL, at TEXT NOT NULL,
  source TEXT);
CREATE TABLE IF NOT EXISTS pm_watch_quotes (slug TEXT NOT NULL, at TEXT NOT NULL, bid REAL, ask REAL,
  PRIMARY KEY (slug, at));
CREATE TABLE IF NOT EXISTS pm_watch_runs (at TEXT PRIMARY KEY, summary_json TEXT NOT NULL);
"""


def _f(v: Any) -> float | None:
    if isinstance(v, dict):
        v = v.get("value")
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0.0 <= x <= 1.0 else None


def _db_path() -> str:
    return os.getenv("NOEMA_PM_WATCH_DB", DEFAULT_DB)


def ensure_schema(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    c.commit()


def watched(main_db: str, now: datetime, cap: int = CAP) -> list[tuple[str, str]]:
    """(slug, reason) in priority order, deduplicated, capped. Read-only on noema.db."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    k = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=10)
    try:
        since = (now - timedelta(hours=6)).isoformat()
        queries = [
            ("links", ("SELECT market_id FROM x_market_links WHERE venue='polymarket-us' AND linked_at>=?"
                       " ORDER BY linked_at DESC"), (since,)),
            ("links", ("SELECT market_id FROM news_market_links WHERE venue='polymarket-us' AND linked_at>=?"
                       " ORDER BY linked_at DESC"), (since,)),
            ("pairs", "SELECT pm_market FROM xv_pairs ORDER BY at DESC", ()),
            ("recent", ("SELECT market_id FROM forecast_ledger WHERE venue='polymarket-us' AND id >"
                        " (SELECT COALESCE(MAX(id),0)-3000 FROM forecast_ledger) ORDER BY id DESC"), ()),
        ]
        for reason, sql, args in queries:
            try:
                rows = k.execute(sql, args).fetchall()
            except sqlite3.Error:
                continue
            for (slug,) in rows:
                if slug and slug not in seen:
                    seen.add(slug)
                    out.append((str(slug), reason))
                    if len(out) >= cap:
                        return out
    finally:
        k.close()
    return out


_CLIENT: dict[str, Any] = {}
BATCH = 100  # slugs per markets.list call (the list response carries bestBidQuote/bestAskQuote)


def _client() -> Any:
    if "c" not in _CLIENT:
        from polymarket_us import PolymarketUS  # public market data only
        _CLIENT["c"] = PolymarketUS(timeout=10.0, max_retries=0)
    return _CLIENT["c"]


def quotes_from_markets(markets: list[dict[str, Any]]) -> dict[str, tuple[float | None, float | None]]:
    out: dict[str, tuple[float | None, float | None]] = {}
    for m in markets or []:
        if not isinstance(m, dict) or not m.get("slug") or m.get("closed") is True:
            continue
        b, a = _f(m.get("bestBidQuote")), _f(m.get("bestAskQuote"))
        if b is not None and a is not None and b > a:
            b = a = None
        out[str(m["slug"])] = (b, a)
    return out


def fetch_batch(slugs: list[str]) -> dict[str, tuple[float | None, float | None]]:
    """Top of book for many markets in ceil(n/100) public list calls (vs one BBO call per market).

    The public API is rate limited per IP (~5 requests / 15 s observed, shared with every NOEMA
    process), so per-market BBO polling cannot keep more than a handful of markets fresh.
    """
    out: dict[str, tuple[float | None, float | None]] = {}
    for i in range(0, len(slugs), BATCH):
        part = slugs[i:i + BATCH]
        d = _client().markets.list({"slug": part, "limit": len(part)})
        out.update(quotes_from_markets((d or {}).get("markets") or []))
    return out


def run_once(main_db: str, db: str | None = None, *, now: datetime | None = None,
             fetch: Any = None) -> dict[str, Any]:
    fixed_now = now
    now = now or datetime.now(UTC)
    fetch = fetch or fetch_batch
    items = watched(main_db, now)
    reasons = dict(items)
    t = time.monotonic()
    error = None
    try:
        quotes = fetch([s_ for s_, _ in items]) if items else {}
    except Exception as exc:  # noqa: BLE001 - logged in the run summary; next cycle retries
        quotes, error = {}, type(exc).__name__
    fetch_s = round(time.monotonic() - t, 2)
    at = (fixed_now or datetime.now(UTC)).isoformat()
    ok = [(s_, reasons.get(s_, "?"), b, a) for s_, (b, a) in quotes.items() if b is not None or a is not None]
    by_reason: dict[str, int] = {}
    for _, r, _, _ in ok:
        by_reason[r] = by_reason.get(r, 0) + 1
    summary = {"at": at, "watched": len(items), "quoted": len(ok), "error": error, "by_reason": by_reason,
               "fetch_s": fetch_s}
    c = sqlite_connect(db or _db_path())
    try:
        ensure_schema(c)
        c.executemany("INSERT OR REPLACE INTO pm_watch_latest VALUES (?,?,?,?,?)",
                      [(s_, b, a, at, r) for s_, r, b, a in ok])
        c.executemany("INSERT OR IGNORE INTO pm_watch_quotes VALUES (?,?,?,?)",
                      [(s_, at, b, a) for s_, _, b, a in ok])
        c.execute("INSERT OR REPLACE INTO pm_watch_runs VALUES (?,?)", (at, json.dumps(summary)))
        cutoff = (datetime.fromisoformat(at) - timedelta(hours=KEEP_H)).isoformat()
        c.execute("DELETE FROM pm_watch_quotes WHERE at < ?", (cutoff,))
        c.execute("DELETE FROM pm_watch_runs WHERE at < ?", (cutoff,))
        c.commit()
    finally:
        c.close()
    return summary


# ------------------------------------------------------------------ readers (never raise on a missing DB)

def _ro(db: str | None) -> sqlite3.Connection | None:
    path = db or _db_path()
    if not Path(path).exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def latest_many(slugs: list[str], *, max_age_s: float = 45, now: datetime | None = None,
                db: str | None = None) -> dict[str, tuple[float | None, float | None, float]]:
    """Watched quotes no older than max_age_s: slug -> (bid, ask, quoted_at_epoch)."""
    if not slugs:
        return {}
    k = _ro(db)
    if k is None:
        return {}
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(seconds=max_age_s)).isoformat()
    try:
        out: dict[str, tuple[float | None, float | None, float]] = {}
        for i in range(0, len(slugs), 400):
            part = slugs[i:i + 400]
            q = ",".join("?" * len(part))
            for slug, b, a, at in k.execute(f"SELECT slug, bid, ask, at FROM pm_watch_latest WHERE slug IN ({q}) AND at>=?"
                                        f" AND at<=?", (*part, cutoff, now.isoformat())):
                out[slug] = (b, a, datetime.fromisoformat(at).timestamp())
        return out
    except sqlite3.Error:
        return {}
    finally:
        k.close()


def quote_at(slug: str, at: datetime, *, tolerance_s: float = 90, db: str | None = None
             ) -> tuple[float | None, float | None] | None:
    """Last watched quote at or before `at` (within tolerance), for pre-event marks."""
    k = _ro(db)
    if k is None:
        return None
    try:
        row = k.execute("SELECT bid, ask FROM pm_watch_quotes WHERE slug=? AND at<=? AND at>=? ORDER BY at DESC"
                        " LIMIT 1", (slug, at.isoformat(), (at - timedelta(seconds=tolerance_s)).isoformat())
                        ).fetchone()
        return (row[0], row[1]) if row else None
    except sqlite3.Error:
        return None
    finally:
        k.close()


def main() -> None:  # pragma: no cover - process entry
    ap = argparse.ArgumentParser()
    ap.add_argument("--main-db", default=os.environ.get("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    while True:
        t = time.monotonic()
        try:
            print(json.dumps(run_once(a.main_db, a.db)), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"at": datetime.now(UTC).isoformat(), "error": f"{type(exc).__name__}: {exc}"[:200]}),
                  flush=True)
        if not a.loop:
            return
        time.sleep(max(5.0, INTERVAL_S - (time.monotonic() - t)))


if __name__ == "__main__":
    main()
