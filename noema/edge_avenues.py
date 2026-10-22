"""Extra edge avenues, PAPER ONLY (no orders, no flags, no keys). Own DB: data/edge_avenues.db.

1. funding_carry - long spot / short perp when trailing funding (strictly before t) clears
   round-trip costs + buffer. Public Kraken Futures + Hyperliquid funding history.
2. kalshi_mm     - passive two-sided quotes 1c inside the spread on fs_quotes snapshots; a fill only
   when a later public trade prints STRICTLY THROUGH our price (taker_side opposite), 1 contract per
   side per snapshot, |inventory| <= 10, Kalshi maker fee 0.0175*p*(1-p), leftover settled at result.
3. news_lag      - news/X items linked to markets (news_quote_marks): if mid moved >= 2c between t0 and
   +300s, buy in that direction at the +300s ask/bid, exit at the +3600s bid/ask, taker fees both legs.
Every entry passes the fee-plus-buffer gate (edge_strategies.clears_fee_buffer, default 1c/contract).
Outcomes land in `paper_outcomes`; agent_lifecycle scores them prospectively (after hire) per kind.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from . import edge_mining as em
from .edge_strategies import clears_fee_buffer, entry_buffer, kalshi_fee
from .sqlite_util import connect as sqlite_connect

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS funding (venue TEXT, coin TEXT, ts INTEGER, rate REAL, PRIMARY KEY (venue, coin, ts));
CREATE TABLE IF NOT EXISTS mm_trades (trade_id TEXT PRIMARY KEY, ticker TEXT, ts REAL, yes_price REAL,
  count REAL, taker_side TEXT);
CREATE TABLE IF NOT EXISTS mm_fetched (ticker TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS paper_outcomes (kind TEXT, id TEXT, at TEXT, net REAL, x_json TEXT,
  PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS carry_positions (venue TEXT, coin TEXT, opened_ts INTEGER, closed_ts INTEGER,
  funding REAL DEFAULT 0, PRIMARY KEY (venue, coin, opened_ts));
CREATE TABLE IF NOT EXISTS avenue_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, report_json TEXT);
"""
KINDS = ("funding_carry", "kalshi_mm", "news_lag", "pm_weather", "pm_thin", "pm_politics")


def connect(path: str) -> sqlite3.Connection:
    c = sqlite_connect(path)
    c.executescript(SCHEMA)
    return c


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def summarize(xs: list[float]) -> dict[str, Any]:
    if not xs:
        return {"n": 0}
    lo, hi, p = em.bootstrap(xs) if len(xs) >= 2 else (None, None, 1.0)
    return {"n": len(xs), "mean": round(sum(xs) / len(xs), 5), "total": round(sum(xs), 4),
            "ci95": [round(lo, 5), round(hi, 5)] if lo is not None else None, "p_le_0": round(p, 4),
            "win_rate": round(sum(x > 0 for x in xs) / len(xs), 3)}


# ------------------------------------------------------------------ 1. funding carry
COINS = {"kraken": {"BTC": "PF_XBTUSD", "ETH": "PF_ETHUSD", "SOL": "PF_SOLUSD"},
         "hyperliquid": {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL"}}
# per-leg taker fee + slippage, fraction of notional (spot leg, perp leg); conservative public tiers
LEG_COST = {"kraken": (0.0026 + 0.0005, 0.0005 + 0.0005), "hyperliquid": (0.0007 + 0.0005, 0.00045 + 0.0005)}
LOOKBACK_H, MIN_HOLD_H = 72, 168
CARRY_BUFFER = 0.001  # 0.1% of notional over the planned hold, on top of round-trip cost


def fetch_funding(c: sqlite3.Connection, client: Any, days: int = 365) -> dict[str, int]:
    out = {}
    now = int(time.time())
    for coin, sym in COINS["kraken"].items():
        try:
            r = client.get("https://futures.kraken.com/derivatives/api/v4/historicalfundingrates", params={"symbol": sym})
            rows = [("kraken", coin, int(_ts(x["timestamp"])), float(x["relativeFundingRate"])) for x in r.json()["rates"]]
            c.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?,?)", rows)
            out[f"kraken:{coin}"] = len(rows)
        except Exception as exc:  # noqa: BLE001
            out[f"kraken:{coin}"] = f"error {type(exc).__name__}"
    for coin in list(COINS["hyperliquid"]):
        last = c.execute("SELECT MAX(ts) FROM funding WHERE venue='hyperliquid' AND coin=?", (coin,)).fetchone()[0]
        start = (last + 1) * 1000 if last else (now - days * 86400) * 1000
        n = 0
        for _ in range(40):
            try:
                rows = client.post("https://api.hyperliquid.xyz/info",
                                   json={"type": "fundingHistory", "coin": coin, "startTime": start}).json()
            except Exception:  # noqa: BLE001
                break
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?,?)",
                          [("hyperliquid", coin, int(x["time"]) // 1000 // 3600 * 3600, float(x["fundingRate"])) for x in rows])
            n += len(rows)
            start = int(rows[-1]["time"]) + 1
            if len(rows) < 500:
                break
        out[f"hyperliquid:{coin}"] = n
    c.commit()
    return out


EXPECT_H = 720  # forward agent: expected hold used to amortize the round trip (fixed a priori, not fitted)


def carry_gate(venue: str, trailing_mean_h: float, horizon_h: int = EXPECT_H) -> bool:
    """Expected funding over horizon_h (from the strictly-past mean) must clear round-trip cost + buffer."""
    s, p = LEG_COST[venue]
    return trailing_mean_h * horizon_h >= 2 * (s + p) + CARRY_BUFFER


def backtest_carry(c: sqlite3.Connection) -> dict[str, Any]:
    return {f"horizon_{h}h": _backtest_carry(c, h) for h in (168, EXPECT_H)}


def _backtest_carry(c: sqlite3.Connection, horizon: int) -> dict[str, Any]:
    """Hourly walk: decision at hour t uses rates with ts < t only. Trade = one open..close episode;
    net = funding collected - round-trip cost (fraction of notional). Positive-carry only (no borrow)."""
    res: dict[str, Any] = {}
    all_trades: list[float] = []
    for venue, coins in COINS.items():
        s, p = LEG_COST[venue]
        rt = 2 * (s + p)
        for coin in coins:
            rates = c.execute("SELECT ts, rate FROM funding WHERE venue=? AND coin=? ORDER BY ts", (venue, coin)).fetchall()
            trades, on, acc, held, win = [], False, 0.0, 0, []
            on_hours = 0
            for i, (_ts_, r) in enumerate(rates):
                if i >= LOOKBACK_H:
                    m = sum(x[1] for x in rates[i - LOOKBACK_H:i]) / LOOKBACK_H  # strictly before this hour
                    if not on and carry_gate(venue, m, horizon):
                        on, acc, held = True, 0.0, 0
                    elif on and held >= MIN_HOLD_H and m <= 0:
                        trades.append(acc - rt)
                        on = False
                if on:
                    acc += r  # short perp receives +rate on notional this hour
                    held += 1
                    on_hours += 1
                win.append(r)
            if on:
                trades.append(acc - rt)  # mark open position closed at end
            yr = len(rates) / 8760 if rates else 0
            res[f"{venue}:{coin}"] = {"hours": len(rates), "years": round(yr, 2),
                                      "mean_hourly_rate": round(sum(x[1] for x in rates) / len(rates), 8) if rates else None,
                                      "round_trip_cost": rt, "time_in_market": round(on_hours / len(rates), 3) if rates else 0,
                                      **summarize(trades),
                                      "net_per_year": round(sum(trades) / yr, 4) if yr else None}
            all_trades += trades
    return {"per_market": res, "all": summarize(all_trades),
            "note": "net is fraction of notional per episode, after spot+perp taker fees and slippage both ways"}


def forward_carry(c: sqlite3.Connection, now: int) -> int:
    """Open/close paper carry positions on the latest data; closed episodes -> paper_outcomes."""
    n = 0
    for venue, coins in COINS.items():
        s, p = LEG_COST[venue]
        for coin in coins:
            rates = c.execute("SELECT ts, rate FROM funding WHERE venue=? AND coin=? AND ts < ? ORDER BY ts DESC LIMIT ?",
                              (venue, coin, now, LOOKBACK_H)).fetchall()
            if len(rates) < LOOKBACK_H:
                continue
            m = sum(r for _, r in rates) / LOOKBACK_H
            pos = c.execute("SELECT opened_ts FROM carry_positions WHERE venue=? AND coin=? AND closed_ts IS NULL",
                            (venue, coin)).fetchone()
            if pos is None and carry_gate(venue, m):
                c.execute("INSERT INTO carry_positions (venue, coin, opened_ts) VALUES (?,?,?)", (venue, coin, now))
            elif pos is not None:
                got = c.execute("SELECT COALESCE(SUM(rate),0) FROM funding WHERE venue=? AND coin=? AND ts>=? AND ts<?",
                                (venue, coin, pos[0], now)).fetchone()[0]
                c.execute("UPDATE carry_positions SET funding=? WHERE venue=? AND coin=? AND opened_ts=?", (got, venue, coin, pos[0]))
                if now - pos[0] >= MIN_HOLD_H * 3600 and m <= 0:
                    c.execute("UPDATE carry_positions SET closed_ts=? WHERE venue=? AND coin=? AND opened_ts=?", (now, venue, coin, pos[0]))
                    c.execute("INSERT OR IGNORE INTO paper_outcomes VALUES (?,?,?,?,?)",
                              ("funding_carry", f"{venue}:{coin}:{pos[0]}", datetime.fromtimestamp(now, UTC).isoformat(),
                               got - 2 * (s + p), json.dumps({"venue": venue, "coin": coin})))
                    n += 1
    c.commit()
    return n


# ------------------------------------------------------------------ 2. Kalshi passive MM
def maker_fee(p: float) -> float:
    return 0.0175 * p * (1 - p)


def fetch_mm_trades(c: sqlite3.Connection, main_db: str, client: Any, limit: int = 150) -> int:
    k = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=30)
    try:
        ticks = [r[0] for r in k.execute("SELECT ticker FROM fs_markets WHERE result IN ('yes','no') ORDER BY close_time DESC")]
    finally:
        k.close()
    done = {r[0] for r in c.execute("SELECT ticker FROM mm_fetched")}
    n = 0
    for t in [t for t in ticks if t not in done][:limit]:
        cursor = None
        for _ in range(10):
            try:
                params = {"ticker": t, "limit": 1000, **({"cursor": cursor} if cursor else {})}
                j = client.get(f"{KALSHI}/markets/trades", params=params).json()
            except Exception:  # noqa: BLE001
                break
            rows = [(x["trade_id"], t, _ts(x["created_time"]), float(x["yes_price_dollars"]), float(x.get("count_fp") or 0),
                     x["taker_side"]) for x in j.get("trades", [])]
            c.executemany("INSERT OR IGNORE INTO mm_trades VALUES (?,?,?,?,?,?)", rows)
            n += len(rows)
            cursor = j.get("cursor")
            if not cursor or not rows:
                break
        c.execute("INSERT OR REPLACE INTO mm_fetched VALUES (?,?)", (t, datetime.now(UTC).isoformat()))
        c.commit()
    return n


def simulate_mm(quotes: list[tuple[float, float, float]], trades: list[tuple[float, float, str]], result: int,
                close_ts: float, inv_cap: int = 10) -> dict[str, Any] | None:
    """quotes: (ts, bid, ask) sorted; trades: (ts, yes_price, taker_side) sorted. Returns per-market P&L."""
    if not quotes or not trades:
        return None
    tts = [t[0] for t in trades]
    mids = [(q[0], (q[1] + q[2]) / 2) for q in quotes]
    mts = [m[0] for m in mids]
    inv, cash, fills, adverse = 0, 0.0, 0, []
    for i, (ts, bid, ask) in enumerate(quotes):
        end = min(quotes[i + 1][0] if i + 1 < len(quotes) else ts + 120, ts + 120)
        if close_ts - ts < 120 or bid is None or ask is None:
            continue
        mb, ma = round(bid + 0.01, 4), round(ask - 0.01, 4)
        if ma - mb - maker_fee(mb) - maker_fee(ma) < entry_buffer() or not (0.05 <= mb and ma <= 0.95):
            continue  # fee-plus-buffer gate on the round-trip capture
        did_b = did_s = False
        for j in range(bisect_right(tts, ts), len(trades)):
            t, px, side = trades[j]
            if t > end:
                break
            if not did_b and side == "no" and px < mb and inv < inv_cap:
                inv += 1
                cash -= mb + maker_fee(mb)
                did_b, fills = True, fills + 1
                k = bisect_right(mts, t + 60)
                if k < len(mids):
                    adverse.append(mids[k][1] - mb)
            if not did_s and side == "yes" and px > ma and inv > -inv_cap:
                inv -= 1
                cash += ma - maker_fee(ma)
                did_s, fills = True, fills + 1
                k = bisect_right(mts, t + 60)
                if k < len(mids):
                    adverse.append(ma - mids[k][1])
    if not fills:
        return None
    pnl = cash + inv * result
    return {"pnl": pnl, "fills": fills, "end_inventory": inv, "adverse_60s": adverse}


def backtest_mm(c: sqlite3.Connection, main_db: str, since: str | None = None) -> dict[str, Any]:
    k = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=30)
    try:
        mk = {r[0]: (1 if r[1] == "yes" else 0, _ts(r[2])) for r in
              k.execute("SELECT ticker, result, close_time FROM fs_markets WHERE result IN ('yes','no')")}
        q = defaultdict(list)
        for t, at, b, a in k.execute("SELECT ticker, at, bid, ask FROM fs_quotes ORDER BY at"):
            if t in mk and b is not None and a is not None:
                q[t].append((_ts(at), b, a))
    finally:
        k.close()
    tr = defaultdict(list)
    for t, ts, px, side in c.execute("SELECT ticker, ts, yes_price, taker_side FROM mm_trades ORDER BY ts"):
        tr[t].append((ts, px, side))
    per, fills, adverse, out = [], 0, [], []
    for t in tr:
        if t not in mk or t not in q:
            continue
        r = simulate_mm(q[t], tr[t], *mk[t])
        if r:
            per.append(r["pnl"])
            fills += r["fills"]
            adverse += r["adverse_60s"]
            out.append((t, r, mk[t][1]))
    return {"markets_with_fills": len(per), "fills": fills, "per_market_pnl_usd_1lot": summarize(per),
            "pnl_per_fill": round(sum(per) / fills, 5) if fills else None,
            "adverse_selection_60s": summarize(adverse),
            "markets_with_trades": len(tr), "_rows": out}


# ------------------------------------------------------------------ 3. news / X lag
def backtest_news(main_db: str, move: float = 0.02) -> dict[str, Any]:
    k = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=30)
    try:
        marks = defaultdict(dict)
        for kind, iid, v, m, t0, off, bid, ask, mid in k.execute(
                "SELECT item_kind, item_id, venue, market_id, t0, offset_s, bid, ask, mid FROM news_quote_marks WHERE status='ok'"):
            marks[(kind, iid, v, m, t0)][off] = (bid, ask, mid)
        capt = dict(k.execute("SELECT item_kind, COUNT(*) FROM news_quote_marks WHERE offset_s=0 GROUP BY 1").fetchall())
        ok0 = dict(k.execute("SELECT item_kind, COUNT(*) FROM news_quote_marks WHERE offset_s=0 AND status='ok' GROUP BY 1").fetchall())
    finally:
        k.close()
    lag = defaultdict(list)
    trades, rows = [], []
    for key, mk in marks.items():
        if 0 not in mk or mk[0][2] is None:
            continue
        m0 = mk[0][2]
        for off in (60, 300, 900, 3600):
            if off in mk and mk[off][2] is not None:
                lag[off].append(abs(mk[off][2] - m0))
        if 300 in mk and 3600 in mk and None not in mk[300] and None not in mk[3600]:
            d = mk[300][2] - m0
            if abs(d) >= move:
                if d > 0:
                    px, ex = mk[300][1], mk[3600][0]
                    edge = d - kalshi_fee(px) - kalshi_fee(ex)  # naive continuation expectation
                    net = ex - px - kalshi_fee(px) - kalshi_fee(ex)
                else:
                    px, ex = 1 - mk[300][0], 1 - mk[3600][1]
                    edge = -d - kalshi_fee(px) - kalshi_fee(ex)
                    net = ex - px - kalshi_fee(px) - kalshi_fee(ex)
                if clears_fee_buffer(edge):
                    trades.append(net)
                    rows.append((key, net))
    return {"items_with_t0_mark": {k_: ok0.get(k_, 0) for k_ in capt}, "items_scheduled": capt,
            "abs_mid_move_from_t0": {off: summarize(v) for off, v in lag.items()},
            "continuation_trades": summarize(trades), "_rows": rows,
            "note": "fee = Kalshi taker formula on both legs (PM US fee is similar); move threshold 2c at +300s"}


# ------------------------------------------------------------------ driver
def _record(c: sqlite3.Connection, kind: str, rows: list[tuple[str, str, float, dict]]) -> None:
    c.executemany("INSERT OR IGNORE INTO paper_outcomes VALUES (?,?,?,?,?)",
                  [(kind, i, at, net, json.dumps(x)) for i, at, net, x in rows])


RUN_COUNTER = {"n": 0}


def _due(kind: str, life_db: str | None) -> bool:
    """Throttle losing / fired kinds: run them every Nth cycle (agent_lifecycle.throttle_factor)."""
    from .agent_lifecycle import throttle_factor
    return RUN_COUNTER["n"] % throttle_factor(life_db, kind) == 0


def run(db: str, main_db: str, *, fetch: bool = True, life_db: str | None = None) -> dict[str, Any]:
    import httpx
    c = connect(db)
    RUN_COUNTER["n"] += 1
    due = {k: _due(k, life_db) for k in ("funding_carry", "kalshi_mm", "news_lag")}
    rep: dict[str, Any] = {"schema": "tev.edge_avenues.v1", "at": datetime.now(UTC).isoformat(), "mode": "paper only",
                           "buffer": entry_buffer()}
    rep["throttled"] = [k for k, v in due.items() if not v]
    if fetch:
        with httpx.Client(timeout=20) as cl:
            if due["funding_carry"]:
                rep["fetched_funding"] = fetch_funding(c, cl)
            if due["kalshi_mm"]:
                rep["fetched_mm_trades"] = fetch_mm_trades(c, main_db, cl)
    rep["funding_carry"] = backtest_carry(c)
    rep["forward_carry_closed"] = forward_carry(c, int(time.time()) // 3600 * 3600)
    mm = backtest_mm(c, main_db)
    _record(c, "kalshi_mm", [(t, datetime.fromtimestamp(ct, UTC).isoformat(), r["pnl"], {"fills": r["fills"]})
                           for t, r, ct in mm.pop("_rows")])
    rep["kalshi_mm"] = mm
    nw = backtest_news(main_db)
    _record(c, "news_lag", [("|".join(k[:4]), datetime.fromtimestamp(_ts(k[4]) + 3600, UTC).isoformat(), net,
                                  {"venue": k[2]}) for k, net in nw.pop("_rows")])
    rep["news_lag"] = nw
    c.execute("INSERT INTO avenue_reports (at, report_json) VALUES (?,?)", (rep["at"], json.dumps(rep)))
    c.commit()
    c.close()
    return rep


def load_samples(db: str | None) -> dict[str, list[dict[str, Any]]]:
    """Per-kind samples for agent_lifecycle (same shape as trench samples)."""
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    if not db or not os.path.isfile(db):
        return out
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        for kind, i, at, net, xj in c.execute("SELECT kind, id, at, net, x_json FROM paper_outcomes ORDER BY at"):
            out.setdefault(kind, []).append({"id": i, "at": at, "x": json.loads(xj or "{}"), "net": net, "gross": net, "liq": 0,
                                             "disposition": kind})
    except sqlite3.Error:
        pass
    finally:
        c.close()
    return out


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/edge_avenues.db")
    ap.add_argument("--main-db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--life-db", default=os.getenv("NOEMA_LIFECYCLE_DB", "data/agent_lifecycle.db"))
    a = ap.parse_args()
    while True:
        try:
            rep = run(a.db, a.main_db, fetch=not a.no_fetch, life_db=a.life_db)
            print(json.dumps({"at": rep["at"], "carry": {h: v["all"] for h, v in rep["funding_carry"].items()},
                              "mm": rep["kalshi_mm"]["per_market_pnl_usd_1lot"],
                              "news": rep["news_lag"]["continuation_trades"]}), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:200]}), flush=True)
        if not a.loop:
            return
        time.sleep(a.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
