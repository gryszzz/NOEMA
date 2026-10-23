"""Fast-feedback forecasting loop (paper / forecast only, never trades).

Every cycle:
  1. discover short-dated Kalshi markets (closing in <= FS_HORIZON_H hours) with a
     two-sided quote, via the public read-only market list (no auth, no keys);
  2. snapshot quotes of tracked markets until they close;
  3. let a small panel of cheap forecaster agents log ONE forecast per market
     strictly before close into accuracy_forecasts (market_price = mid);
  4. read official settlements after close and score them with
     accuracy_ledger.resolve_open_forecasts (Brier vs market price), which feeds
     the accuracy board and agent_lifecycle.score_forecasters (hire/fire);
  5. capture quotes of markets linked to news / X items at -5m, 0, +1m, +5m,
     +15m, +60m for news-latency testing.

Rate limits: Kalshi basic tier allows ~20 reads/s; this loop makes a handful of
paged list calls every DISCOVER_EVERY seconds plus batched ticker reads, with a
minimum gap between requests. Never reads signer material, never places orders.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .accuracy_ledger import ensure_accuracy_schema, record_forecast, resolve_open_forecasts
from .sqlite_util import connect as sqlite_connect
from .sqlite_util import is_busy

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com"
VENUE = "kalshi"
HORIZON_H = float(os.environ.get("NOEMA_FS_HORIZON_HOURS", "6"))
DISCOVER_EVERY = int(os.environ.get("NOEMA_FS_DISCOVER_SECONDS", "600"))
CYCLE_SECONDS = int(os.environ.get("NOEMA_FS_CYCLE_SECONDS", "60"))
MAX_TRACKED = int(os.environ.get("NOEMA_FS_MAX_TRACKED", "600"))
MIN_GAP_S = 0.25  # <= 4 req/s, well under Kalshi's public read limit
MARK_OFFSETS = (-300, 0, 60, 300, 900, 3600)
AGENTS = ("fs-market-mid", "fs-longshot-fade", "fs-momentum", "fs-spot-vol")
CRYPTO = {"KXBTC": "BTC-USD", "KXETH": "ETH-USD", "KXSOL": "SOL-USD", "KXXRP": "XRP-USD",
          "KXDOGE": "DOGE-USD"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS fs_markets (ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT,
    category TEXT, title TEXT, subtitle TEXT, strike_type TEXT, floor_strike REAL, cap_strike REAL,
    close_time TEXT NOT NULL, discovered_at TEXT NOT NULL, result TEXT, settled_at TEXT,
    resolved_forecasts INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS fs_markets_close ON fs_markets(close_time);
CREATE TABLE IF NOT EXISTS fs_quotes (ticker TEXT NOT NULL, at TEXT NOT NULL, bid REAL, ask REAL,
    last REAL, PRIMARY KEY (ticker, at));
CREATE TABLE IF NOT EXISTS fs_forecasts (ticker TEXT NOT NULL, agent_id TEXT NOT NULL,
    forecast_id INTEGER NOT NULL, at TEXT NOT NULL, PRIMARY KEY (ticker, agent_id));
CREATE TABLE IF NOT EXISTS news_quote_marks (item_kind TEXT NOT NULL, item_id TEXT NOT NULL,
    venue TEXT NOT NULL, market_id TEXT NOT NULL, t0 TEXT NOT NULL, offset_s INTEGER NOT NULL,
    due_at TEXT NOT NULL, captured_at TEXT, bid REAL, ask REAL, mid REAL, status TEXT,
    PRIMARY KEY (item_kind, item_id, venue, market_id, offset_s));
CREATE INDEX IF NOT EXISTS nqm_due ON news_quote_marks(captured_at, due_at);
CREATE TABLE IF NOT EXISTS fs_paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
    agent_id TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
    prob REAL NOT NULL, edge REAL NOT NULL, contracts INTEGER NOT NULL, result TEXT, pnl REAL, settled_at TEXT,
    UNIQUE (agent_id, ticker));
CREATE TABLE IF NOT EXISTS fs_books (ticker TEXT NOT NULL, at TEXT NOT NULL, best_bid REAL, best_ask REAL,
    bid_depth5 REAL, ask_depth5 REAL, imbalance REAL, seconds_to_close REAL, PRIMARY KEY (ticker, at));
CREATE TABLE IF NOT EXISTS fs_trades (trade_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, created_time TEXT NOT NULL,
    yes_price REAL, count REAL, taker_side TEXT);
CREATE TABLE IF NOT EXISTS fs_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
    summary_json TEXT NOT NULL);
"""


def ensure_schema(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    ensure_accuracy_schema(c)


def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


def category(series: str) -> str:
    s = series.upper()
    if any(s.startswith(k) for k in CRYPTO) or "CRYPTO" in s or s.startswith(("KXHYPE", "KXBNB")):
        return "crypto"
    if s.startswith(("KXINX", "KXNASDAQ", "KXDOW", "KXRUT", "KXUSDJPY", "KXEURUSD", "KXGOLD",
                     "KXSILVER", "KXWTI", "KXBRENT", "KXNATGAS", "KXTNOTE", "KXCOPPER")):
        return "markets"
    if s.startswith(("KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXTEMP", "KXWIND")):
        return "weather"
    if s.startswith(("KXAAAGAS", "KXDIESEL", "KXCPI", "KXJOBLESS", "KXPAYROLL", "KXGDP", "KXFED",
                     "KXEIA", "KXMORTGAGE", "KXCBD")):
        return "economics"
    if s.startswith(("KXTRUTH", "KXTWEET", "KXMENTION", "KXSAY")):
        return "politics/culture"
    if "MATCH" in s or "GAME" in s or s.startswith(("KXNFL", "KXNBA", "KXMLB", "KXNHL", "KXNCAA",
                                                     "KXATP", "KXWTA", "KXEPL", "KXUFC", "KXMLS")):
        return "sports"
    return "other"


RETRY_STATUS = {429, 500, 502, 503, 504}
_SECRETISH = re.compile(r"(?i)(key|token|secret|signature|authorization|bearer)[^,;&\s]*[=:]\s*[^,;&\s]+")


def _safe_body(r: httpx.Response, limit: int = 200) -> str:
    """First bytes of an error body for diagnostics; anything secret-shaped is redacted."""
    try:
        text = r.text[:limit]
    except Exception:  # noqa: BLE001
        return ""
    return _SECRETISH.sub(r"\1=<redacted>", text.replace("\n", " "))


def _log_http(event: str, **fields: Any) -> None:
    fields.update(event=event, ts=datetime.now(UTC).isoformat(timespec="seconds"))
    print(json.dumps(fields, default=str), file=sys.stderr, flush=True)


class Api:
    """Public Kalshi/Coinbase reads with a minimum gap, jittered backoff and bounded retries.

    Retries 429 / 5xx / timeouts / transport errors (honoring Retry-After), and a 403 once
    (Kalshi's edge returns transient 403s under burst). Each failure is logged with the HTTP
    status and the first bytes of the body (secret-shaped substrings redacted; requests carry
    no credentials).
    """

    def __init__(self, client: httpx.Client | None = None, *, attempts: int = 4,
                 base_backoff: float = 1.0, sleep: Any = time.sleep) -> None:
        self.c = client or httpx.Client(timeout=httpx.Timeout(20, connect=8),
                                        headers={"accept": "application/json"})
        self.last = 0.0
        self.calls = 0
        self.attempts = attempts
        self.base_backoff = base_backoff
        self.sleep = sleep
        self.errors: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.errors[key] = self.errors.get(key, 0) + 1

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        try:
            ra = float(retry_after) if retry_after else 0.0
        except ValueError:
            ra = 0.0
        d = max(ra, self.base_backoff * 2 ** attempt) * (0.5 + random.random())
        return min(30.0, d)

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        path = httpx.URL(url).path
        forbidden_retried = False
        last_exc: Exception | None = None
        for attempt in range(self.attempts):
            wait = MIN_GAP_S - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            try:
                r = self.c.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._count(type(exc).__name__)
                _log_http("fs_http_error", path=path, error=type(exc).__name__, attempt=attempt + 1)
                last_exc = exc
                self.sleep(self._backoff(attempt))
                continue
            if r.status_code < 400:
                return r.json()
            self._count(str(r.status_code))
            _log_http("fs_http_error", path=path, status=r.status_code, attempt=attempt + 1,
                      body=_safe_body(r))
            retryable = r.status_code in RETRY_STATUS or (r.status_code == 403 and not forbidden_retried)
            if r.status_code == 403:
                forbidden_retried = True
            if not retryable or attempt == self.attempts - 1:
                r.raise_for_status()
            self.sleep(self._backoff(attempt, r.headers.get("retry-after")))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("kalshi request retries exhausted")


def _quote(m: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    return _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars")), _f(m.get("last_price_dollars"))


def eligible(m: dict[str, Any]) -> bool:
    b, a, _ = _quote(m)
    return (m.get("market_type") == "binary" and b is not None and a is not None
            and b >= 0.02 and a <= 0.98 and a - b <= 0.10 and a > b)


def discover(c: sqlite3.Connection, api: Api, now: datetime, max_pages: int = 20) -> int:
    lo, hi = int(now.timestamp()) + 120, int((now + timedelta(hours=HORIZON_H)).timestamp())
    cursor, added, pages = None, 0, 0
    tracked = c.execute("SELECT COUNT(*) FROM fs_markets WHERE result IS NULL").fetchone()[0]
    while pages < max_pages:
        p = {"status": "open", "limit": 1000, "min_close_ts": lo, "max_close_ts": hi, "mve_filter": "exclude"}
        if cursor:
            p["cursor"] = cursor
        data = api.get(f"{KALSHI}/markets", p)
        pages += 1
        for m in data.get("markets") or []:
            if tracked >= MAX_TRACKED or not eligible(m):
                continue
            series = (m.get("event_ticker") or m["ticker"]).split("-")[0]
            cur = c.execute(
                "INSERT OR IGNORE INTO fs_markets (ticker, event_ticker, series, category, title, subtitle,"
                " strike_type, floor_strike, cap_strike, close_time, discovered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (m["ticker"], m.get("event_ticker"), series, category(series), m.get("title"),
                 m.get("yes_sub_title") or m.get("subtitle"), m.get("strike_type"), _f(m.get("floor_strike")),
                 _f(m.get("cap_strike")), _dt(m["close_time"]).isoformat(), now.isoformat()))
            if cur.rowcount:
                added += 1
                tracked += 1
            b, a, last = _quote(m)
            c.execute("INSERT OR IGNORE INTO fs_quotes VALUES (?,?,?,?,?)", (m["ticker"], now.isoformat(), b, a, last))
        c.commit()  # never hold the noema.db write lock across the next page fetch
        cursor = data.get("cursor")
        if not cursor:
            break
    return added


def fetch_by_tickers(api: Api, tickers: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(tickers), 100):
        data = api.get(f"{KALSHI}/markets", {"tickers": ",".join(tickers[i:i + 100]), "limit": 100})
        for m in data.get("markets") or []:
            out[m["ticker"]] = m
    return out


def snapshot(c: sqlite3.Connection, api: Api, now: datetime) -> dict[str, dict[str, Any]]:
    rows = [r[0] for r in c.execute("SELECT ticker FROM fs_markets WHERE result IS NULL AND close_time > ?",
                                    (now.isoformat(),))]
    live = fetch_by_tickers(api, rows) if rows else {}
    for t, m in live.items():
        b, a, last = _quote(m)
        c.execute("INSERT OR IGNORE INTO fs_quotes VALUES (?,?,?,?,?)", (t, now.isoformat(), b, a, last))
    return live


_spot_cache: dict[str, tuple[float, float, float]] = {}


def spot_and_vol(api: Api, product: str) -> tuple[float, float] | None:
    """Spot and per-sqrt-second realized vol from 1-minute Coinbase candles (public)."""
    hit = _spot_cache.get(product)
    if hit and time.monotonic() - hit[2] < 55:
        return hit[0], hit[1]
    try:
        candles = api.get(f"{COINBASE}/products/{product}/candles", {"granularity": 60})
    except Exception:  # noqa: BLE001
        return None
    closes = [float(x[4]) for x in sorted(candles, key=lambda x: x[0])][-240:]
    if len(closes) < 30:
        return None
    rets = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
    var = sum(r * r for r in rets) / len(rets)
    vol = math.sqrt(var / 60.0) * 1.15  # small fat-tail allowance
    _spot_cache[product] = (closes[-1], vol, time.monotonic())
    return closes[-1], vol


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def spot_probability(row: sqlite3.Row, spot: float, vol: float, seconds: float) -> float | None:
    sd = max(vol * math.sqrt(max(seconds, 1.0)), 1e-6)
    def above(k: float) -> float:
        return _phi(math.log(spot / k) / sd) if k > 0 else 1.0
    st, lo, hi = (row["strike_type"] or ""), row["floor_strike"], row["cap_strike"]
    if st in ("greater", "greater_or_equal") and lo:
        p = above(lo)
    elif st in ("less", "less_or_equal") and hi:
        p = 1 - above(hi)
    elif st == "between" and lo and hi:
        p = above(lo) - above(hi)
    else:
        return None
    return min(0.99, max(0.01, p))


def _clip(p: float) -> float:
    return min(0.99, max(0.01, p))


def forecasts_for(c: sqlite3.Connection, api: Api, row: sqlite3.Row, mid: float, now: datetime) -> dict[str, tuple[float, dict]]:
    out: dict[str, tuple[float, dict]] = {"fs-market-mid": (mid, {"rule": "market mid"})}
    fade = mid * 0.85 if mid < 0.15 else (1 - (1 - mid) * 0.85 if mid > 0.85 else mid)
    out["fs-longshot-fade"] = (_clip(fade), {"rule": "shade tails 15% toward certainty (favourite-longshot bias)"})
    old = c.execute("SELECT bid, ask FROM fs_quotes WHERE ticker=? AND at<=? AND bid IS NOT NULL AND ask IS NOT NULL"
                    " ORDER BY at DESC LIMIT 1", (row["ticker"], (now - timedelta(minutes=20)).isoformat())).fetchone()
    if old:
        om = (old[0] + old[1]) / 2
        out["fs-momentum"] = (_clip(mid + 0.5 * (mid - om)), {"rule": "mid + 0.5*(20m change)", "mid_20m": om})
    product = next((v for k, v in CRYPTO.items() if row["series"].startswith(k)), None)
    if product and "15M" not in row["series"]:  # 15-minute markets settle vs window-open price, not a strike
        sv = spot_and_vol(api, product)
        if sv:
            secs = (_dt(row["close_time"]) - now).total_seconds()
            p = spot_probability(row, sv[0], sv[1], secs)
            if p is not None:
                out["fs-spot-vol"] = (p, {"rule": "lognormal from Coinbase spot + 4h realized vol",
                                          "spot": sv[0], "vol_per_sqrt_s": sv[1], "seconds_to_close": secs})
    out.update(pm_forecasts(row, mid, now))
    if row["category"] == "crypto":
        try:
            from .pm_crypto import live_forecasts
            b, a = PM.quotes.get(row["ticker"], (mid, mid))
            out.update(live_forecasts(PM_DB, row, mid, b, a, now))
        except Exception:  # noqa: BLE001, S110
            pass
    return out


def capture_microstructure(c: sqlite3.Connection, api: Api, now: datetime, horizon_s: int = 3600) -> dict[str, int]:
    """Public order books for crypto markets closing within the hour, plus the public trade tape."""
    rows = c.execute("SELECT ticker, close_time FROM fs_markets WHERE result IS NULL AND category='crypto' AND"
                     " close_time > ? AND close_time <= ?", (now.isoformat(),
                     (now + timedelta(seconds=horizon_s)).isoformat())).fetchall()
    books = 0
    for t, close in rows[:40]:
        try:
            ob = api.get(f"{KALSHI}/markets/{t}/orderbook").get("orderbook_fp") or {}
        except Exception:  # noqa: BLE001, S112
            continue
        yes = sorted(((_f(p), _f(q)) for p, q in ob.get("yes_dollars") or []), key=lambda x: -(x[0] or 0))
        no = sorted(((_f(p), _f(q)) for p, q in ob.get("no_dollars") or []), key=lambda x: -(x[0] or 0))
        bd, ad = sum(q or 0 for _, q in yes[:5]), sum(q or 0 for _, q in no[:5])
        c.execute("INSERT OR IGNORE INTO fs_books VALUES (?,?,?,?,?,?,?,?)", (
            t, now.isoformat(), yes[0][0] if yes else None, (1 - no[0][0]) if no and no[0][0] is not None else None,
            bd, ad, (bd - ad) / (bd + ad) if bd + ad else None, (_dt(close) - now).total_seconds()))
        books += 1
        c.commit()  # never hold the noema.db write lock across the next HTTP fetch
    tracked = {r[0] for r in c.execute("SELECT ticker FROM fs_markets WHERE category='crypto' AND result IS NULL")}
    last = c.execute("SELECT MAX(created_time) FROM fs_trades").fetchone()[0]
    since = int(_dt(last).timestamp()) if last else int((now - timedelta(minutes=10)).timestamp())
    trades = 0
    c.commit()
    try:
        d = api.get(f"{KALSHI}/markets/trades", {"limit": 1000, "min_ts": since})
        for tr in d.get("trades") or []:
            if tr.get("ticker") in tracked:
                trades += c.execute("INSERT OR IGNORE INTO fs_trades VALUES (?,?,?,?,?,?)", (
                    tr.get("trade_id"), tr["ticker"], tr.get("created_time"),
                    _f(tr.get("yes_price_dollars")), _f(tr.get("count_fp")), tr.get("taker_side"))).rowcount
    except Exception:  # noqa: BLE001, S110
        pass
    return {"books": books, "trades": trades}


class _PMContext:
    """Trained per-category agents from pm_models (read from the training DB, refreshed every 5 min)."""

    def __init__(self) -> None:
        self.loaded_at = 0.0
        self.fs: Any = None
        self.agents: list[dict[str, Any]] = []
        self.quotes: dict[str, tuple[float, float]] = {}


PM = _PMContext()
PM_DB = os.environ.get("NOEMA_PM_TRAIN_DB", "/workspace/NOEMA/data/pm_train.db")


def pm_refresh(force: bool = False) -> None:
    if not os.path.isfile(PM_DB) or (not force and time.monotonic() - PM.loaded_at < 300):
        return
    from .pm_history import refresh_live
    from .pm_models import FeatureSource
    c = sqlite_connect(PM_DB)
    try:
        refresh_live(c)
        c.execute("CREATE TABLE IF NOT EXISTS pm_agents (agent_id TEXT PRIMARY KEY, genome_json TEXT NOT NULL,"
                  " status TEXT NOT NULL, generation INTEGER NOT NULL, parent_id TEXT, hired_at TEXT NOT NULL,"
                  " fired_at TEXT, fire_reason TEXT, stats_json TEXT, weights_json TEXT, trade_enabled INTEGER DEFAULT 0)")
        PM.fs = FeatureSource(c)
        PM.agents = [{"id": a, "g": json.loads(g), "w": json.loads(w) if w else None, "trade": bool(t)}
                     for a, g, w, t in c.execute("SELECT agent_id, genome_json, weights_json, trade_enabled FROM"
                                                 " pm_agents WHERE status != 'fired'")]
    finally:
        c.close()
    PM.loaded_at = time.monotonic()


def pm_forecasts(row: sqlite3.Row, mid: float, now: datetime) -> dict[str, tuple[float, dict]]:
    if not PM.agents or PM.fs is None:
        return {}
    from .pm_models import predict
    secs = (_dt(row["close_time"]) - now).total_seconds()
    lead = 6 * 3600 if row["category"] in ("weather", "economics") else 2 * 3600
    if secs > lead:  # agents were trained on quotes at most `lead` before close; wait until comparable
        return {}
    b, a = PM.quotes.get(row["ticker"], (mid, mid))
    r = {"series": row["series"], "event_ticker": row["event_ticker"] or "", "snap_at": now.isoformat(),
         "close_time": row["close_time"], "strike_type": row["strike_type"], "floor_strike": row["floor_strike"],
         "cap_strike": row["cap_strike"], "category": row["category"], "snap_mid": mid, "snap_bid": b, "snap_ask": a}
    m = PM.fs.model(r)
    r["model_kind"], r["model_p"] = m if m else (None, None)
    out: dict[str, tuple[float, dict]] = {}
    for ag in PM.agents:
        if ag["g"]["category"] != row["category"]:
            continue
        p = predict(ag["g"]["model"], ag["w"], r)
        if p is not None:
            out[ag["id"]] = (p, {"rule": f"pm_models {ag['g']['model']}", "genome": ag["g"], "model_p": r["model_p"],
                                 "trade_enabled": ag["trade"]})
    return out


def paper_trade(c: sqlite3.Connection, agent: str, why: dict, ticker: str, p: float, b: float, a: float, now: datetime) -> None:
    """Paper only: record a 10-contract trade when the trained agent is cleared (OOS P&L CI > 0) and edge > fee."""
    if not why.get("trade_enabled"):
        return
    from .pm_models import kalshi_fee
    if p - a > kalshi_fee(a):
        side, price, edge = "yes", a, p - a - kalshi_fee(a)
    elif (1 - p) - (1 - b) > kalshi_fee(1 - b):
        side, price, edge = "no", 1 - b, (1 - p) - (1 - b) - kalshi_fee(1 - b)
    else:
        return
    from .edge_strategies import clears_fee_buffer
    if not clears_fee_buffer(edge):
        return
    c.execute("INSERT OR IGNORE INTO fs_paper_trades (at, agent_id, ticker, side, price, fee, prob, edge, contracts)"
              " VALUES (?,?,?,?,?,?,?,?,?)", (now.isoformat(), agent, ticker, side, price, kalshi_fee(price), p, edge, 10))


def forecast(c: sqlite3.Connection, api: Api, live: dict[str, dict[str, Any]], now: datetime) -> int:
    c.row_factory = sqlite3.Row
    written = 0
    rows = c.execute("SELECT * FROM fs_markets WHERE result IS NULL AND close_time > ?",
                     ((now + timedelta(minutes=2)).isoformat(),)).fetchall()
    for row in rows:
        m = live.get(row["ticker"])
        if not m or not eligible(m):
            continue
        b, a, _ = _quote(m)
        mid = round((b + a) / 2, 4)
        PM.quotes[row["ticker"]] = (b, a)
        done = {r[0] for r in c.execute("SELECT agent_id FROM fs_forecasts WHERE ticker=?", (row["ticker"],))}
        for agent, (p, why) in forecasts_for(c, api, row, mid, now).items():
            if agent in done:
                continue
            fid = record_forecast(
                c, agent_id=agent, venue=VENUE, market_id=row["ticker"], predicted_probability=round(p, 4),
                reasoning={**why, "bid": b, "ask": a}, recorded_at=now, symbol=row["series"],
                market_price=mid, decision="benchmark" if agent == "fs-market-mid" else "paper_forecast",
                horizon=f"{int((_dt(row['close_time']) - now).total_seconds() // 60)}m",
                metadata={"kind": "binary", "source": "fast_settle", "category": row["category"],
                          "close_time": row["close_time"], "bid": b, "ask": a, "paper_only": True})
            c.execute("INSERT INTO fs_forecasts VALUES (?,?,?,?)", (row["ticker"], agent, fid, now.isoformat()))
            if agent.startswith("pm-"):
                paper_trade(c, agent, why, row["ticker"], p, b, a, now)
            written += 1
        c.commit()  # forecasts_for() may call Coinbase for the next market
    c.row_factory = None
    return written


def settle(c: sqlite3.Connection, api: Api, now: datetime, db_path: str | None) -> tuple[int, int]:
    rows = [r for r in c.execute("SELECT ticker, close_time FROM fs_markets WHERE result IS NULL AND close_time <= ?"
                                 " AND close_time >= ?", (now.isoformat(), (now - timedelta(days=3)).isoformat()))]
    if not rows:
        return 0, 0
    data = fetch_by_tickers(api, [r[0] for r in rows])
    markets = scored = 0
    for t, close in rows:
        m = data.get(t)
        if not m:
            continue
        res = (m.get("result") or "").lower()
        if m.get("status") not in ("finalized", "settled", "determined") or res not in ("yes", "no"):
            if m.get("status") in ("finalized", "settled") and res:
                c.execute("UPDATE fs_markets SET result=?, settled_at=? WHERE ticker=?", (res, now.isoformat(), t))
            continue
        y = 1 if res == "yes" else 0
        c.execute("UPDATE fs_paper_trades SET result=?, settled_at=?, pnl=contracts*((CASE side WHEN 'yes' THEN ?"
                  " ELSE ? END) - price - fee) WHERE ticker=? AND result IS NULL", (res, now.isoformat(), y, 1 - y, t))
        n = resolve_open_forecasts(c, venue=VENUE, market_id=t, outcome_yes=1 if res == "yes" else 0,
                                   resolved_at=close, source="kalshi_settlement", db_path=db_path)
        c.execute("UPDATE fs_markets SET result=?, settled_at=?, resolved_forecasts=? WHERE ticker=?",
                  (res, now.isoformat(), n, t))
        markets += 1
        scored += n
    return markets, scored


# ---------------- news / X latency marks ----------------

def schedule_marks(c: sqlite3.Connection, now: datetime) -> int:
    since = (now - timedelta(minutes=10)).isoformat()
    added = 0
    for kind, table, idcol in (("news", "news_market_links", "evidence_id"), ("x", "x_market_links", "post_id")):
        try:
            rows = c.execute(f"SELECT {idcol}, venue, market_id, linked_at FROM {table} WHERE linked_at >= ?",
                             (since,)).fetchall()
        except sqlite3.OperationalError:
            continue
        for item, venue, mid, linked in rows:
            t0 = _dt(linked)
            for off in MARK_OFFSETS:
                cur = c.execute("INSERT OR IGNORE INTO news_quote_marks (item_kind, item_id, venue, market_id, t0,"
                                " offset_s, due_at) VALUES (?,?,?,?,?,?,?)",
                                (kind, item, venue, mid, t0.isoformat(), off,
                                 (t0 + timedelta(seconds=off)).isoformat()))
                added += cur.rowcount
    return added


PM_BUDGET = int(os.environ.get("NOEMA_FS_PM_BUDGET", "200"))  # distinct PM-US markets quoted per cycle
TOKEN_SETTLE_S = 120  # a 1m Coinbase candle is final ~2 min after its minute starts
_CB_MISSING: set[str] = set()  # symbols without a Coinbase <SYM>-USD product


def _mark_late(off: int, late: float) -> bool:
    return late > max(90, off * 0.25)


def _pm_quotes(slugs: list[str], now: datetime) -> dict[str, tuple[float | None, float | None, str]]:
    """Live PM-US top of book: pm_watch (<= 45 s old) first, then one batched public list call
    per 100 slugs (bestBidQuote/bestAskQuote), never one BBO call per market (per-IP rate limit)."""
    from . import pm_watch
    out: dict[str, tuple[float | None, float | None, str]] = {}
    try:
        for slug, (b, a, _at) in pm_watch.latest_many(slugs, max_age_s=45, now=now).items():
            out[slug] = (b, a, "ok")
    except Exception:  # noqa: BLE001, S110 - the watcher is an accelerator, never a dependency
        pass
    todo = [s_ for s_ in slugs if s_ not in out][:PM_BUDGET]
    if todo:
        try:
            got = pm_watch.fetch_batch(todo)
        except Exception as exc:  # noqa: BLE001
            got = {}
            err = f"error:{type(exc).__name__}"
            for s_ in todo:
                out[s_] = (None, None, err)
        for s_ in todo:
            if s_ in got:
                out[s_] = (*got[s_], "ok")
            elif s_ not in out:
                out[s_] = (None, None, "error:not_listed")
    return out


def _cb_symbol(sym: str) -> str | None:
    sym = (sym or "").upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", sym) or sym in _CB_MISSING or sym in {"USDC", "USDT", "USD"}:
        return None
    return f"{sym}-USD"


def token_prices(api: Api, sym: str, times: list[datetime]) -> dict[datetime, float | None]:
    """USD price at each time from public Coinbase 1-minute candles (open of the minute containing t).

    Coinbase returns <= 300 candles per call, so times are grouped into <= 290-minute windows.
    A symbol without a Coinbase USD product returns {} and is remembered as unpriceable.
    """
    product = _cb_symbol(sym)
    if not product or not times:
        return {}
    out: dict[datetime, float | None] = {}
    ts = sorted(set(times))
    i = 0
    while i < len(ts):
        start = ts[i].replace(second=0, microsecond=0) - timedelta(minutes=5)
        end = start + timedelta(minutes=290)
        group = [t for t in ts[i:] if t < end]
        i += len(group)
        try:
            rows = api.get(f"{COINBASE}/products/{product}/candles",
                           {"granularity": 60, "start": start.isoformat(), "end": end.isoformat()})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404):
                _CB_MISSING.add(sym.upper())
                return {}
            raise
        opens = {int(r[0]): float(r[3]) for r in rows or [] if isinstance(r, list) and len(r) >= 5}
        for t in group:
            m = int(t.timestamp()) // 60 * 60
            px = next((opens[k] for k in (m, m - 60, m - 120) if k in opens), None)
            out[t] = px
    return out


def capture_marks(c: sqlite3.Connection, api: Api, now: datetime) -> int:
    """Measure due news/X marks: read due rows, fetch with no transaction open, write once.

    * Kalshi: live quote (or stored quote / 1m candle for the -5m mark).
    * Polymarket US: pm_watch quote or one batched public list call per 100 markets.
    * Token (X-linked crypto symbols): Coinbase 1m candle at the exact due time - exact even
      when processed late, so token marks are never 'missed' (status 'candle', mid=USD price;
      'unpriceable' when the symbol has no Coinbase USD market).
    """
    due = c.execute("SELECT item_kind, item_id, venue, market_id, t0, offset_s, due_at FROM news_quote_marks"
                    " WHERE captured_at IS NULL AND due_at <= ? ORDER BY due_at LIMIT 600",
                    (now.isoformat(),)).fetchall()
    if c.in_transaction:
        c.commit()
    res: dict[tuple, tuple[float | None, float | None, float | None, str]] = {}
    kalshi_live: set[str] = set()
    pm_live: list[str] = []
    tokens: dict[str, list[tuple[tuple, datetime]]] = {}
    for kind, item, venue, mid, t0, off, due_at in due:
        pk = (kind, item, venue, mid, off)
        late = (now - _dt(due_at)).total_seconds()
        if venue == "token":
            if late < TOKEN_SETTLE_S:
                continue  # candle not final yet; measure next cycle
            if _cb_symbol(mid) is None:
                res[pk] = (None, None, None, "unpriceable")
            else:
                tokens.setdefault(mid.upper(), []).append((pk, _dt(due_at)))
        elif off < 0:
            row = c.execute("SELECT bid, ask FROM fs_quotes WHERE ticker=? AND at BETWEEN ? AND ? ORDER BY at DESC"
                            " LIMIT 1", (mid, (_dt(due_at) - timedelta(minutes=3)).isoformat(), due_at)).fetchone()
            if row:
                res[pk] = (row[0], row[1], None, "history")
            elif venue.startswith("kalshi"):
                b, a, st = _kalshi_candle(api, mid, _dt(due_at))
                res[pk] = (b, a, None, st)
            else:
                hist = None
                try:
                    from . import pm_watch
                    hist = pm_watch.quote_at(mid, _dt(due_at))
                except Exception:  # noqa: BLE001
                    hist = None
                res[pk] = (hist[0], hist[1], None, "history") if hist else (None, None, None, "no_history")
        elif _mark_late(off, late):
            res[pk] = (None, None, None, "missed")
        elif venue.startswith("kalshi"):
            kalshi_live.add(mid)
        else:
            if mid not in pm_live:
                pm_live.append(mid)
    kq: dict[str, dict[str, Any]] = {}
    if kalshi_live:
        try:
            kq = fetch_by_tickers(api, sorted(kalshi_live))
        except Exception as exc:  # noqa: BLE001
            _log_http("fs_marks_kalshi_error", error=type(exc).__name__)
            kq = {}
    pq = _pm_quotes(pm_live, now) if pm_live else {}
    for sym, items in tokens.items():
        try:
            px = token_prices(api, sym, [t for _, t in items])
        except Exception as exc:  # noqa: BLE001 - retry next cycle
            _log_http("fs_marks_token_error", symbol=sym, error=type(exc).__name__)
            continue
        for pk, t in items:
            if _cb_symbol(sym) is None:
                res[pk] = (None, None, None, "unpriceable")
            elif px.get(t) is not None:
                res[pk] = (None, None, px[t], "candle")
            else:
                res[pk] = (None, None, None, "no_history")
    for kind, item, venue, mid, t0, off, due_at in due:
        pk = (kind, item, venue, mid, off)
        if pk in res or venue == "token" or off < 0:
            continue
        late = (now - _dt(due_at)).total_seconds()
        if venue.startswith("kalshi"):
            m = kq.get(mid)
            if m is None:
                if kalshi_live and kq:
                    res[pk] = (None, None, None, "error:not_found")
                continue  # fetch failed: retry next cycle while still on time
            qb, qa, _ = _quote(m)
            res[pk] = (qb, qa, None, "ok")
        else:
            q = pq.get(mid)
            if q is None or q[2].startswith("error"):
                if q is not None and _mark_late(off, late + CYCLE_SECONDS):
                    res[pk] = (None, None, None, q[2])
                continue  # over budget / transient: next cycle while still on time
            res[pk] = (q[0], q[1], None, "ok")
    rows = []
    for (kind, item, venue, mid, off), (b, a, m, st) in res.items():
        if m is None and b is not None and a is not None:
            m = (b + a) / 2
        rows.append((now.isoformat(), b, a, m, st, kind, item, venue, mid, off))
    if rows:
        c.executemany("UPDATE news_quote_marks SET captured_at=?, bid=?, ask=?, mid=?, status=? WHERE item_kind=?"
                      " AND item_id=? AND venue=? AND market_id=? AND offset_s=?", rows)
        c.commit()
    return len(rows)


def backfill_token_marks(c: sqlite3.Connection, api: Api, *, limit: int = 20000) -> dict[str, int]:
    """Re-measure token marks recorded as missed/no_history from exact Coinbase 1m candles."""
    rows = c.execute("SELECT item_kind, item_id, venue, market_id, offset_s, due_at FROM news_quote_marks"
                     " WHERE venue='token' AND status IN ('missed','no_history') AND due_at <= ? LIMIT ?",
                     ((datetime.now(UTC) - timedelta(seconds=TOKEN_SETTLE_S)).isoformat(), limit)).fetchall()
    by_sym: dict[str, list[tuple[tuple, datetime]]] = {}
    for kind, item, venue, mid, off, due_at in rows:
        by_sym.setdefault((mid or "").upper(), []).append(((kind, item, venue, mid, off), _dt(due_at)))
    out = {"candle": 0, "unpriceable": 0, "no_history": 0}
    stamp = datetime.now(UTC).isoformat()
    for sym, items in by_sym.items():
        px = token_prices(api, sym, [t for _, t in items]) if _cb_symbol(sym) else {}
        upd = []
        for pk, t in items:
            if _cb_symbol(sym) is None:
                st, m = "unpriceable", None
            elif px.get(t) is not None:
                st, m = "candle", px[t]
            else:
                st, m = "no_history", None
            out[st] += 1
            upd.append((stamp, m, st, *pk))
        c.executemany("UPDATE news_quote_marks SET captured_at=?, bid=NULL, ask=NULL, mid=?, status=? WHERE"
                      " item_kind=? AND item_id=? AND venue=? AND market_id=? AND offset_s=?", upd)
        c.commit()
    return out


def _kalshi_candle(api: Api, ticker: str, at: datetime) -> tuple[float | None, float | None, str]:
    series = ticker.split("-")[0]
    try:
        d = api.get(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
                    {"start_ts": int(at.timestamp()) - 300, "end_ts": int(at.timestamp()), "period_interval": 1})
        cs = d.get("candlesticks") or []
        if not cs:
            return None, None, "no_history"
        last = cs[-1]
        bid = _f((last.get("yes_bid") or {}).get("close_dollars"))
        ask = _f((last.get("yes_ask") or {}).get("close_dollars"))
        return bid, ask, "candle"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"error:{type(exc).__name__}"


def stats(c: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    day = (now - timedelta(hours=24)).isoformat()
    by_agent = c.execute(
        "SELECT f.agent_id, COUNT(*), AVG((f.predicted_probability-r.outcome)*(f.predicted_probability-r.outcome)),"
        " AVG((f.market_price-r.outcome)*(f.market_price-r.outcome)), SUM(COALESCE(r.ev_after_cost,0))"
        " FROM accuracy_forecasts f JOIN accuracy_resolutions r ON r.forecast_id=f.id"
        " WHERE f.agent_id LIKE 'fs-%' GROUP BY f.agent_id").fetchall()
    return {
        "tracked_open": c.execute("SELECT COUNT(*) FROM fs_markets WHERE result IS NULL AND close_time > ?",
                                  (now.isoformat(),)).fetchone()[0],
        "by_category": dict(c.execute("SELECT category, COUNT(*) FROM fs_markets WHERE result IS NULL AND"
                                      " close_time > ? GROUP BY category", (now.isoformat(),)).fetchall()),
        "markets_settled_24h": c.execute("SELECT COUNT(*) FROM fs_markets WHERE result IN ('yes','no') AND"
                                         " settled_at >= ?", (day,)).fetchone()[0],
        "forecasts_resolved_24h": c.execute("SELECT COALESCE(SUM(resolved_forecasts),0) FROM fs_markets WHERE"
                                            " settled_at >= ?", (day,)).fetchone()[0],
        "agents": [{"agent": a, "n": n, "brier": round(b, 4), "market_brier": round(mb, 4),
                    "paper_ev_after_cost": round(ev or 0, 3)} for a, n, b, mb, ev in by_agent],
        "pm_agents_live": len(PM.agents),
        "paper_trades": dict(c.execute("SELECT COALESCE(result,'open'), COUNT(*) FROM fs_paper_trades GROUP BY 1").fetchall()),
        "paper_pnl": c.execute("SELECT ROUND(COALESCE(SUM(pnl),0),2) FROM fs_paper_trades").fetchone()[0],
        "news_marks": dict(c.execute("SELECT COALESCE(status,'pending'), COUNT(*) FROM news_quote_marks"
                                     " GROUP BY status").fetchall()),
    }


def _phase(c: sqlite3.Connection, out: dict[str, Any], name: str, fn: Any, default: Any = None,
           attempts: int = 3) -> Any:
    """Run one phase; a busy/locked DB rolls back and retries, then degrades to '<name>_error'.

    Every phase is idempotent (INSERT OR IGNORE / guarded UPDATEs), so a retry never double counts,
    and one phase losing the lock race no longer discards the whole cycle.
    """
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            if i == attempts - 1:
                out[f"{name}_error"] = "database_busy"
                out.setdefault("lock_errors", 0)
                out["lock_errors"] += 1
                return default
            time.sleep(min(8.0, 0.5 * 2 ** i) * (0.5 + random.random()))
    return default


def run_once(db: str, api: Api | None = None, now: datetime | None = None, *, force_discover: bool = False) -> dict[str, Any]:
    api = api or Api()
    now = now or datetime.now(UTC)
    c = sqlite_connect(db)
    try:
        _phase(c, {}, "schema", lambda: (ensure_schema(c), c.commit()), attempts=6)
        out: dict[str, Any] = {"at": now.isoformat()}
        last = c.execute("SELECT MAX(at) FROM fs_runs WHERE summary_json LIKE '%\"discovered\"%'").fetchone()[0]
        if force_discover or not last or (now - _dt(last)).total_seconds() >= DISCOVER_EVERY:
            out["discovered"] = _phase(c, out, "discover", lambda: (discover(c, api, now), c.commit())[0], 0)
        live = _phase(c, out, "snapshot", lambda: (snapshot(c, api, now), c.commit())[0], {})
        try:
            pm_refresh()
        except Exception as exc:  # noqa: BLE001
            out["pm_error"] = f"{type(exc).__name__}: {exc}"[:200]
        out["microstructure"] = _phase(c, out, "microstructure",
                                       lambda: (capture_microstructure(c, api, now), c.commit())[0], {})
        out["forecasts_written"] = _phase(c, out, "forecast", lambda: (forecast(c, api, live, now), c.commit())[0], 0)
        out["settled_markets"], out["resolved_forecasts"] = _phase(
            c, out, "settle", lambda: (settle(c, api, now, db), c.commit())[0], (0, 0))
        out["marks_scheduled"] = _phase(c, out, "schedule_marks", lambda: (schedule_marks(c, now), c.commit())[0], 0)
        out["marks_captured"] = _phase(c, out, "capture_marks", lambda: capture_marks(c, api, now), 0)
        out["api_calls"] = api.calls
        out["api_errors"] = dict(getattr(api, "errors", {}))
        out.update(_phase(c, out, "stats", lambda: stats(c, now), {}) or {})
        _phase(c, out, "run_log", lambda: (c.execute("INSERT INTO fs_runs (at, summary_json) VALUES (?,?)",
                                                      (now.isoformat(), json.dumps(out))), c.commit()), attempts=6)
        return out
    finally:
        c.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill-token-marks", action="store_true",
                    help="re-measure missed/no_history token marks from Coinbase 1m candles, then exit")
    a = ap.parse_args()
    api = Api()
    if a.backfill_token_marks:
        c = sqlite_connect(a.db)
        try:
            print(json.dumps(backfill_token_marks(c, api)), flush=True)
        finally:
            c.close()
        return
    first = True
    while True:
        t = time.monotonic()
        try:
            print(json.dumps(run_once(a.db, api, force_discover=first)), flush=True)
            first = False
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"at": datetime.now(UTC).isoformat(), "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        api.calls = 0
        api.errors = {}
        if not a.loop:
            return
        time.sleep(max(5.0, CYCLE_SECONDS - (time.monotonic() - t)))


if __name__ == "__main__":
    main()
