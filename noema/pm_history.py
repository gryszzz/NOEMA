"""Historical training set for short-dated Kalshi markets (read-only, public data only).

Backfills settled Kalshi markets for the fast-settling series (crypto, indexes/FX/
commodities, AAA gas, weather, econ) with the market's own quote at a fixed
pre-close snapshot (1-minute candlesticks), plus the reference price history and
archived weather forecasts needed by the per-category models in pm_models.

Nothing here trades, signs, or reads secrets. Rate limits: Kalshi <= 4 req/s,
Yahoo chart 1 call per symbol, Open-Meteo 1 call per city.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .fast_settle import KALSHI, Api, _f, category

SERIES = [
    # crypto (hourly/daily ranges + thresholds, 15-minute up/down)
    "KXBTCD", "KXBTC", "KXETHD", "KXETH", "KXSOLD", "KXSOLE", "KXXRPD", "KXXRP", "KXDOGED", "KXDOGE",
    "KXBNBD", "KXBNB", "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXBNB15M",
    # indexes / commodities / FX
    "KXINX", "KXINXU", "KXNASDAQ100", "KXNASDAQ100U", "KXGOLDH", "KXSILVERH", "KXWTIH", "KXGOLD15M",
    "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M", "KXEURUSD15M", "KXUSDJPY15M",
    "KXGBPUSD15M", "KXAUDUSD15M", "KXUSDCAD15M", "KXEURUSDH", "KXUSDJPYH",
    # weather
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHPHIL", "KXHIGHTHOU",
    "KXHIGHTPHX", "KXHIGHTBOS", "KXHIGHTDC", "KXLOWTNYC", "KXLOWTHOU", "KXLOWTDC", "KXLOWTBOS", "KXLOWTPHX",
    "KXRAIN", "KXRAINNYC",
    # gas / econ
    "KXAAAGASD", "KXAAAGASW", "KXDIESELD", "KXJOBLESSCLAIMS", "KXCPI", "KXPAYROLLS", "KXEIAGAS",
]

# Reference instruments (Yahoo chart symbols). Futures/FX proxies carry basis noise vs Kalshi's index.
REFERENCE = [("KXBTC", "BTC-USD"), ("KXETH", "ETH-USD"), ("KXSOL", "SOL-USD"), ("KXXRP", "XRP-USD"),
             ("KXDOGE", "DOGE-USD"), ("KXBNB", "BNB-USD"), ("KXINX", "^GSPC"), ("KXNASDAQ100", "^NDX"),
             ("KXGOLD", "GC=F"), ("KXSILVER", "SI=F"), ("KXWTI", "CL=F"), ("KXBRENT", "BZ=F"),
             ("KXNATGAS", "NG=F"), ("KXCOPPER", "HG=F"), ("KXEURUSD", "EURUSD=X"), ("KXUSDJPY", "JPY=X"),
             ("KXGBPUSD", "GBPUSD=X"), ("KXAUDUSD", "AUDUSD=X"), ("KXUSDCAD", "CAD=X")]

# Settlement stations (NWS climate sites Kalshi cites) for daily high/low temperature series.
CITIES = {"NY": (40.783, -73.967), "NYC": (40.783, -73.967), "CHI": (41.786, -87.752), "MIA": (25.788, -80.317),
          "LAX": (33.938, -118.389), "AUS": (30.183, -97.680), "DEN": (39.847, -104.656), "PHIL": (39.873, -75.227),
          "HOU": (29.638, -95.282), "PHX": (33.428, -112.004), "BOS": (42.361, -71.010), "DC": (38.848, -77.034)}


def reference_symbol(series: str) -> str | None:
    return next((sym for k, sym in REFERENCE if series.startswith(k)), None)


def weather_site(series: str) -> tuple[str, str, tuple[float, float]] | None:
    for kind, pref in (("high", "KXHIGHT"), ("high", "KXHIGH"), ("low", "KXLOWT")):
        if series.startswith(pref):
            code = series[len(pref):]
            if code in CITIES:
                return kind, code, CITIES[code]
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_train (ticker TEXT PRIMARY KEY, series TEXT NOT NULL, category TEXT NOT NULL,
    event_ticker TEXT, open_time TEXT, close_time TEXT NOT NULL, snap_at TEXT, strike_type TEXT,
    floor_strike REAL, cap_strike REAL, result INTEGER NOT NULL, expiration_value REAL, volume REAL,
    snap_bid REAL, snap_ask REAL, snap_mid REAL, fetched_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pm_train_close ON pm_train(close_time);
CREATE TABLE IF NOT EXISTS pm_ref_bars (symbol TEXT NOT NULL, ts INTEGER NOT NULL, close REAL NOT NULL,
    PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS pm_weather_fc (site TEXT NOT NULL, day TEXT NOT NULL, tmax REAL, tmin REAL,
    fetched_at TEXT NOT NULL, PRIMARY KEY (site, day));
CREATE TABLE IF NOT EXISTS pm_backfill_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
    summary_json TEXT NOT NULL);
"""


def ensure_schema(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp())


def snapshot_lead(cat: str, open_ts: int, close_ts: int) -> int:
    """Seconds before close at which the training quote is taken (mirrors fast_settle's horizon)."""
    cap = 6 * 3600 if cat in ("weather", "economics") else 2 * 3600
    return max(60, min(cap, (close_ts - open_ts) // 2))


def list_candidates(c: sqlite3.Connection, api: Api, series: str, since: datetime, *,
                    per_event: int = 8, max_markets: int = 3000) -> list[dict[str, Any]]:
    cursor, rows = None, []
    while True:
        p: dict[str, Any] = {"series_ticker": series, "status": "settled", "limit": 1000,
                             "min_close_ts": int(since.timestamp())}
        if cursor:
            p["cursor"] = cursor
        try:
            d = api.get(f"{KALSHI}/markets", p)
        except httpx.HTTPStatusError:
            return []
        rows += [m for m in d.get("markets") or [] if m.get("result") in ("yes", "no")
                 and m.get("market_type") == "binary"]
        cursor = d.get("cursor")
        if not cursor or len(rows) >= max_markets * 3:
            break
    # Keep the most-traded strikes per event: those are where the market actually had an opinion.
    by_event: dict[str, list[dict[str, Any]]] = {}
    for m in rows:
        by_event.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)
    keep = []
    for ms in by_event.values():
        ms.sort(key=lambda m: -(_f(m.get("volume_fp")) or 0))
        keep += [m for m in ms[:per_event] if (_f(m.get("volume_fp")) or 0) > 0]
    keep.sort(key=lambda m: m["close_time"], reverse=True)
    have = {r[0] for r in c.execute("SELECT ticker FROM pm_train WHERE series=?", (series,))}
    for m in keep:
        m["_series"] = series
    return [m for m in keep[:max_markets] if m["ticker"] not in have]


def fetch_quotes(c: sqlite3.Connection, api: Api, markets: list[dict[str, Any]]) -> int:
    """Quote each market at its snapshot time; markets sharing a snapshot minute share one batched call."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for m in markets:
        cat = category(m["_series"])
        close_ts = _ts(m["close_time"])
        open_ts = _ts(m.get("open_time") or m["close_time"])
        m["_snap"] = close_ts - snapshot_lead(cat, open_ts, close_ts)
        groups.setdefault(m["_snap"] // 60 * 60, []).append(m)
    added = 0
    now = datetime.now(UTC).isoformat()
    for key, ms in sorted(groups.items()):
        for i in range(0, len(ms), 20):
            chunk = ms[i:i + 20]
            quotes: dict[str, tuple[float | None, float | None]] = {}
            try:
                d = api.get(f"{KALSHI}/markets/candlesticks", {
                    "market_tickers": ",".join(m["ticker"] for m in chunk), "start_ts": key - 900,
                    "end_ts": key + 59, "period_interval": 1})
            except httpx.HTTPError:
                continue
            for x in d.get("markets") or []:
                best: tuple[float | None, float | None] = (None, None)
                for cs in x.get("candlesticks") or []:
                    b = _f((cs.get("yes_bid") or {}).get("close_dollars"))
                    a = _f((cs.get("yes_ask") or {}).get("close_dollars"))
                    if b is not None and a is not None and a > b:
                        best = (b, a)
                quotes[x["market_ticker"]] = best
            for m in chunk:
                b, a = quotes.get(m["ticker"], (None, None))
                c.execute("INSERT OR IGNORE INTO pm_train VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    m["ticker"], m["_series"], category(m["_series"]), m.get("event_ticker"), m.get("open_time"),
                    m["close_time"], datetime.fromtimestamp(m["_snap"], UTC).isoformat(), m.get("strike_type"),
                    _f(m.get("floor_strike")), _f(m.get("cap_strike")), 1 if m["result"] == "yes" else 0,
                    _f(m.get("expiration_value")), _f(m.get("volume_fp")), b, a,
                    None if b is None or a is None else round((b + a) / 2, 4), now))
                added += 1
        c.commit()
    return added


def backfill_reference(c: sqlite3.Connection, client: httpx.Client) -> dict[str, int]:
    out = {}
    for _, sym in REFERENCE:
        try:
            r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                           params={"interval": "5m", "range": "60d"}, headers={"User-Agent": "Mozilla/5.0"})
            res = r.json()["chart"]["result"][0]
            ts, closes = res["timestamp"], res["indicators"]["quote"][0]["close"]
        except Exception:  # noqa: BLE001
            out[sym] = 0
            continue
        rows = [(sym, t, float(v)) for t, v in zip(ts, closes, strict=False) if v]
        c.executemany("INSERT OR REPLACE INTO pm_ref_bars VALUES (?,?,?)", rows)
        out[sym] = len(rows)
        time.sleep(0.5)
    c.commit()
    return out


def backfill_weather(c: sqlite3.Connection, client: httpx.Client, since: datetime) -> int:
    n = 0
    end = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    for code, (lat, lon) in CITIES.items():
        try:
            d = client.get("https://historical-forecast-api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "start_date": since.date().isoformat(), "end_date": end,
                "daily": "temperature_2m_max,temperature_2m_min", "temperature_unit": "fahrenheit",
                "timezone": "America/New_York"}).json()["daily"]
        except Exception:  # noqa: BLE001, S112
            continue
        for day, hi, lo in zip(d["time"], d["temperature_2m_max"], d["temperature_2m_min"], strict=False):
            c.execute("INSERT OR REPLACE INTO pm_weather_fc VALUES (?,?,?,?,?)",
                      (code, day, hi, lo, datetime.now(UTC).isoformat()))
            n += 1
        time.sleep(0.3)
    c.commit()
    return n


def run(db: str, days: int = 45, series: list[str] | None = None, max_per_series: int = 3000) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    c = sqlite3.connect(db, timeout=30)
    ensure_schema(c)
    api = Api()
    client = httpx.Client(timeout=30)
    summary: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "days": days, "series": {}}
    summary["reference_bars"] = backfill_reference(c, client)
    summary["weather_days"] = backfill_weather(c, client, since)
    cands: list[dict[str, Any]] = []
    for s in series or SERIES:
        got = list_candidates(c, api, s, since, max_markets=max_per_series)
        summary["series"][s] = len(got)
        cands += got
        print(json.dumps({"series": s, "candidates": len(got), "calls": api.calls}), flush=True)
    summary["added"] = fetch_quotes(c, api, cands)
    summary["kalshi_calls"] = api.calls
    summary["train_rows"] = c.execute("SELECT COUNT(*) FROM pm_train").fetchone()[0]
    summary["with_quote"] = c.execute("SELECT COUNT(*) FROM pm_train WHERE snap_mid IS NOT NULL").fetchone()[0]
    c.execute("INSERT INTO pm_backfill_runs (at, summary_json) VALUES (?,?)", (summary["at"], json.dumps(summary)))
    c.commit()
    c.close()
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("NOEMA_PM_TRAIN_DB", "data/pm_train.db"))
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--series", nargs="*")
    a = ap.parse_args()
    print(json.dumps(run(a.db, a.days, a.series)), flush=True)


if __name__ == "__main__":
    main()


def refresh_live(c: sqlite3.Connection, client: httpx.Client | None = None) -> dict[str, int]:
    """Keep the last 2 days of reference bars and today's/tomorrow's weather forecast fresh for live agents."""
    ensure_schema(c)
    client = client or httpx.Client(timeout=20)
    out: dict[str, int] = {}
    for _, sym in REFERENCE:
        try:
            r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                           params={"interval": "5m", "range": "2d"}, headers={"User-Agent": "Mozilla/5.0"})
            res = r.json()["chart"]["result"][0]
            rows = [(sym, t, float(v)) for t, v in zip(res["timestamp"], res["indicators"]["quote"][0]["close"],
                                                      strict=False) if v]
        except Exception:  # noqa: BLE001, S112
            continue
        c.executemany("INSERT OR REPLACE INTO pm_ref_bars VALUES (?,?,?)", rows)
        out[sym] = len(rows)
    for code, (lat, lon) in CITIES.items():
        try:
            d = client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "forecast_days": 2, "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit", "timezone": "America/New_York"}).json()["daily"]
        except Exception:  # noqa: BLE001, S112
            continue
        for day, hi, lo in zip(d["time"], d["temperature_2m_max"], d["temperature_2m_min"], strict=False):
            c.execute("INSERT OR REPLACE INTO pm_weather_fc VALUES (?,?,?,?,?)",
                      (code, day, hi, lo, datetime.now(UTC).isoformat()))
    c.commit()
    return out
