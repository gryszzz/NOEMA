"""Kalshi maker (resting limit) test for the pm_models shadow agents — paper/research only.

Pre-registered in docs/research/kalshi-maker-prereg-2026-10-22.md (committed before any result).
Question: do the near-break-even shadow agents turn profitable if, instead of paying the taker fee and
crossing the spread, they rest a limit at the bid (JOIN) or one tick inside it (IMPROVE)?

Fill model (conservative): an order rests from placement to close-60s and fills only when a recorded trade
prints *through* its price or a recorded quote crosses it. We are last in the queue, so at-price prints never
fill us (reported as a sensitivity). Every fill pays the maker fee 0.0175*P*(1-P) (M=1 upper bound, rounded
up to the centicent) plus a 0.5c adverse-selection haircut, and is held to settlement.

Backtest  : walk-forward OOS predictions (pm_models.walk_forward) on pm_train snapshots, fills from
            edge_avenues.mm_trades + noema.fs_trades + noema.fs_quotes/fs_books (all read-only).
Forward   : the same rules on the agents' live forecasts (accuracy_forecasts) from a start time, for 7 days.
            After a market settles its public trade prints are fetched once (read-only GET) into
            data/kalshi_maker.db. Never places orders, never reads keys, never touches live flags.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import random
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
HB_NAME = "agent:kalshi-maker-sim"
PENALTY = 0.005
PLACE_DELAY_LIVE_S = 5
CANCEL_BEFORE_CLOSE_S = 60
VARIANTS = ("join", "improve")
MIN_FILLS, MIN_EVENTS, MIN_DAYS, MIN_DAY_FILLS = 300, 50, 3, 30
N_BOOT, SEED, ALPHA = 2000, 7, 0.05
FORWARD_DAYS = 7
FINAL_GRACE_H = 24

# Frozen at pre-registration (the 16 non-fired pm_agents genomes on 2026-10-22 22:02 ET).
AGENTS: dict[str, dict[str, Any]] = {
    "pm-crypto-market_cal-1eb3c0": {"category": "crypto", "model": "market_cal", "window_days": 999, "ridge": 1.0},
    "pm-crypto-market_cal-25afbc": {"category": "crypto", "model": "market_cal", "window_days": 999, "ridge": 3.0},
    "pm-crypto-market_cal-40c622": {"category": "crypto", "model": "market_cal", "window_days": 999, "ridge": 0.3},
    "pm-crypto-market_cal-4c72c6": {"category": "crypto", "model": "market_cal", "window_days": 21, "ridge": 3.0},
    "pm-economic-market_cal-3fd698": {"category": "economics", "model": "market_cal", "window_days": 999, "ridge": 1.0},
    "pm-markets-market_cal-359eb4": {"category": "markets", "model": "market_cal", "window_days": 999, "ridge": 1.0},
    "pm-markets-stack-396545": {"category": "markets", "model": "stack", "window_days": 30, "ridge": 3.0},
    "pm-markets-stack-4fb13f": {"category": "markets", "model": "stack", "window_days": 999, "ridge": 1.0},
    "pm-other-market_cal-a30995": {"category": "other", "model": "market_cal", "window_days": 999, "ridge": 1.0},
    "pm-other-stack-248d77": {"category": "other", "model": "stack", "window_days": 7, "ridge": 1.0},
    "pm-other-stack-51783d": {"category": "other", "model": "stack", "window_days": 999, "ridge": 1.0},
    "pm-other-stack-f65274": {"category": "other", "model": "stack", "window_days": 14, "ridge": 3.0},
    "pm-weather-market_cal-3e1a7c": {"category": "weather", "model": "market_cal", "window_days": 999, "ridge": 1.0},
    "pm-weather-market_cal-52ac9d": {"category": "weather", "model": "market_cal", "window_days": 14, "ridge": 10.0},
    "pm-weather-market_cal-b2469c": {"category": "weather", "model": "market_cal", "window_days": 30, "ridge": 1.0},
    "pm-weather-stack-80285d": {"category": "weather", "model": "stack", "window_days": 999, "ridge": 1.0},
}


# ---------------------------------------------------------------- fees and rules
def _ceil_cc(x: float) -> float:
    """Round up to the centicent (0.0001 $), as Kalshi does per order."""
    return math.ceil(round(x * 10000, 6)) / 10000


def maker_fee(p: float, m: float = 1.0) -> float:
    return _ceil_cc(m * 0.0175 * p * (1 - p)) if m else 0.0


def taker_fee(p: float) -> float:
    return _ceil_cc(0.07 * p * (1 - p))


def decide(p: float, bid: float | None, ask: float | None, variant: str, m: float = 1.0) -> tuple[str, float] | None:
    """Maker order for agent probability p. Returns (side, yes_level): side 'yes' rests a YES bid at the level,
    side 'no' rests a YES offer at the level (= buys NO at 1-level). None = no order."""
    if bid is None or ask is None or not ask > bid:
        return None
    if variant == "join":
        lb, la = bid, ask
    elif variant == "improve":
        lb, la = round(bid + 0.01, 4), round(ask - 0.01, 4)
        if not (lb < ask and la > bid):
            return None
    else:
        raise ValueError(variant)
    best: tuple[float, str, float] | None = None
    if 0.01 <= lb <= 0.99:
        e = p - lb - maker_fee(lb, m)
        if e > 0:
            best = (e, "yes", lb)
    if 0.01 <= la <= 0.99:
        e = (1 - p) - (1 - la) - maker_fee(1 - la, m)
        if e > 0 and (best is None or e > best[0]):
            best = (e, "no", la)
    return (best[1], best[2]) if best else None


def net_pnl(side: str, level: float, result: int, m: float = 1.0, penalty: float = PENALTY) -> float:
    if side == "yes":
        return result - level - maker_fee(level, m) - penalty
    px = 1 - level
    return (1 - result) - px - maker_fee(px, m) - penalty


def taker_net(p: float, bid: float, ask: float, result: int) -> float | None:
    """The agents' existing taker rule (pm_models.evaluate) for comparison."""
    if p - ask > taker_fee(ask):
        return result - ask - taker_fee(ask)
    if (1 - p) - (1 - bid) > taker_fee(1 - bid):
        return (1 - result) - (1 - bid) - taker_fee(1 - bid)
    return None


# ---------------------------------------------------------------- evidence
class Evidence:
    """Per-ticker trade prints (ts, yes_price) and quotes (ts, bid, ask), sorted by ts."""

    def __init__(self) -> None:
        self.trades: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.quotes: dict[str, list[tuple[float, float | None, float | None]]] = defaultdict(list)
        self.trade_start: dict[str, float] = {}  # earliest reliable trade ts (truncated mm_trades histories)
        self.partial: dict[str, list[tuple[float, float]]] = defaultdict(list)  # fs_trades-only: proof of fill only
        self._ids: set[str] = set()

    def add_trade(self, tid: str, ticker: str, ts: float, px: float | None) -> None:
        if px is None or tid in self._ids:
            return
        self._ids.add(tid)
        self.trades[ticker].append((ts, px))

    def finish(self) -> None:
        for v in self.trades.values():
            v.sort()
        for v in self.quotes.values():
            v.sort(key=lambda q: q[0])
        for v in self.partial.values():
            v.sort()

    def fill(self, ticker: str, side: str, level: float, t0: float, t1: float,
             at_price: bool = False) -> tuple[bool, bool]:
        """(evaluable, filled) for a resting order in (t0, t1]."""
        tr = self.trades.get(ticker, [])
        qs = self.quotes.get(ticker, [])
        use_tr = bool(tr) and self.trade_start.get(ticker, -math.inf) <= t0
        evaluable = False
        if use_tr:
            i = bisect.bisect_right(tr, (t0, math.inf))
            evaluable = True  # complete prints from before placement: no print-through = no fill
            for ts, px in tr[i:]:
                if ts > t1:
                    break
                if side == "yes" and (px < level - 1e-9 or (at_price and abs(px - level) < 1e-9)):
                    return True, True
                if side == "no" and (px > level + 1e-9 or (at_price and abs(px - level) < 1e-9)):
                    return True, True
        if qs:
            i = bisect.bisect_right([q[0] for q in qs], t0)
            for ts, b, a in qs[i:]:
                if ts > t1:
                    break
                evaluable = True
                if side == "yes" and a is not None and (a < level - 1e-9 or (at_price and a <= level + 1e-9)):
                    return True, True
                if side == "no" and b is not None and (b > level + 1e-9 or (at_price and b >= level - 1e-9)):
                    return True, True
        pt = self.partial.get(ticker, [])
        for ts, px in pt[bisect.bisect_right(pt, (t0, math.inf)):]:
            if ts > t1:
                break
            if side == "yes" and (px < level - 1e-9 or (at_price and abs(px - level) < 1e-9)):
                return True, True
            if side == "no" and (px > level + 1e-9 or (at_price and abs(px - level) < 1e-9)):
                return True, True
        return evaluable, False


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def load_evidence(main_db: str, avenues_db: str | None, tickers: set[str], maker_db: str | None = None) -> Evidence:
    ev = Evidence()
    if avenues_db and os.path.isfile(avenues_db):
        a = sqlite3.connect(f"file:{avenues_db}?mode=ro", uri=True, timeout=30)
        try:
            counts = dict(a.execute("SELECT ticker, COUNT(*) FROM mm_trades GROUP BY ticker").fetchall())
            first = dict(a.execute("SELECT ticker, MIN(ts) FROM mm_trades GROUP BY ticker").fetchall())
            for tid, t, ts, px in a.execute("SELECT trade_id, ticker, ts, yes_price FROM mm_trades"):
                if t in tickers:
                    ev.add_trade(tid, t, ts, px)
            for t, n in counts.items():
                if t in tickers and n >= 10000:  # paged fetch capped at 10 pages: history before first print unknown
                    ev.trade_start[t] = first[t]
        finally:
            a.close()
    if maker_db and os.path.isfile(maker_db):
        k = sqlite3.connect(f"file:{maker_db}?mode=ro", uri=True, timeout=30)
        try:
            if k.execute("SELECT 1 FROM sqlite_master WHERE name='km_trades'").fetchone():
                for tid, t, ts, px in k.execute("SELECT trade_id, ticker, ts, yes_price FROM km_trades"):
                    if t in tickers:
                        ev.add_trade(tid, t, ts, px)
                for t, ts, n in k.execute("SELECT ticker, MIN(ts), COUNT(*) FROM km_trades GROUP BY ticker"):
                    if t in tickers and t in ev.trade_start:
                        ev.trade_start[t] = min(ev.trade_start[t], ts)
                    elif t in tickers and n >= KM_PAGE * KM_MAX_PAGES:  # page cap hit: early history unknown
                        ev.trade_start[t] = ts
        finally:
            k.close()
    c = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=60)
    try:
        # fs_trades is a partial live capture (crypto, near close): it can prove a fill, never a non-fill on its own,
        # so it is merged into prints only for tickers that already have a complete mm/km history.
        have = set(ev.trades)
        for tid, t, ct, px in c.execute("SELECT trade_id, ticker, created_time, yes_price FROM fs_trades"):
            if t in tickers and px is not None:
                if t in have:
                    ev.add_trade(tid, t, _ts(ct), px)
                else:
                    ev.partial[t].append((_ts(ct), px))
        for t, at, b, a in c.execute("SELECT ticker, at, bid, ask FROM fs_quotes"):
            if t in tickers:
                ev.quotes[t].append((_ts(at), b, a))
        for t, at, b, a in c.execute("SELECT ticker, at, best_bid, best_ask FROM fs_books"):
            if t in tickers:
                ev.quotes[t].append((_ts(at), b, a))
    finally:
        c.close()
    ev.finish()
    return ev


# ---------------------------------------------------------------- statistics
def cluster_boot(vals: list[tuple[str, float]], n_boot: int = N_BOOT, seed: int = SEED) -> dict[str, Any]:
    by: dict[str, list[float]] = defaultdict(list)
    for k, v in vals:
        by[k].append(v)
    n = len(vals)
    if not n:
        return {"n": 0, "events": 0, "mean": None, "ci95": None, "p_one_sided": 1.0}
    mean = sum(v for _, v in vals) / n
    keys = list(by)
    sums = [(sum(by[k]), len(by[k])) for k in keys]
    if len(keys) < 5:
        return {"n": n, "events": len(keys), "mean": mean, "ci95": None, "p_one_sided": 1.0}
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        s = m = 0.0
        for _ in keys:
            a, b = sums[rng.randrange(len(keys))]
            s += a
            m += b
        boots.append(s / m)
    boots.sort()
    p = max(sum(b <= 0 for b in boots) / n_boot, 1 / n_boot)
    return {"n": n, "events": len(keys), "mean": mean,
            "ci95": [boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]], "p_one_sided": p}


def holm(pvals: list[float]) -> list[float]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [1.0] * m
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * pvals[i]))
        adj[i] = run
    return adj


def verdicts(results: list[dict[str, Any]]) -> None:
    """Apply the pre-registered criteria in place (Holm over every agent x variant in `results`)."""
    adj = holm([r["stats"]["p_one_sided"] for r in results])
    for r, pa in zip(results, adj, strict=True):
        s = r["stats"]
        days = {d: v for d, v in r["by_day"].items() if v["n"] >= MIN_DAY_FILLS}
        crit = {
            "mean_pos_ci_excludes_0": bool(s["mean"] is not None and s["mean"] > 0 and s["ci95"] and s["ci95"][0] > 0),
            "holm_p_lt_0.05": pa < ALPHA,
            "min_sample": s["n"] >= MIN_FILLS and s["events"] >= MIN_EVENTS and len(days) >= MIN_DAYS,
            "every_day_positive": bool(days) and all(v["mean"] > 0 for v in days.values()),
        }
        r["p_holm"] = pa
        r["criteria"] = crit
        if all(crit.values()):
            r["verdict"] = "PASS"
        elif s["mean"] is not None and s["mean"] > 0 and not crit["min_sample"]:
            r["verdict"] = "promising, under-sampled"
        else:
            r["verdict"] = "fail"


def _round(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 5)
    if isinstance(x, list):
        return [_round(v) for v in x]
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    return x


# ---------------------------------------------------------------- simulation core
def simulate(cases: list[dict[str, Any]], ev: Evidence) -> list[dict[str, Any]]:
    """cases: {agent, ticker, event, day, p, bid, ask, result, t_place, t_cancel}. One row per agent x variant."""
    out = []
    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cs in cases:
        by_agent[cs["agent"]].append(cs)
    for agent in AGENTS:
        rows = by_agent.get(agent, [])
        for variant in VARIANTS:
            fills: list[tuple[str, float]] = []
            per_day: dict[str, list[float]] = defaultdict(list)
            sens = {"fee_m0": [], "at_price_fills": [], "no_penalty": []}
            taker_same: list[tuple[str, float]] = []
            orders = evaluable = 0
            for cs in rows:
                d = decide(cs["p"], cs["bid"], cs["ask"], variant)
                if not d:
                    continue
                orders += 1
                side, lvl = d
                okv, filled = ev.fill(cs["ticker"], side, lvl, cs["t_place"], cs["t_cancel"])
                okv2, filled2 = ev.fill(cs["ticker"], side, lvl, cs["t_place"], cs["t_cancel"], at_price=True)
                if okv2 and filled2:
                    sens["at_price_fills"].append((cs["event"], net_pnl(side, lvl, cs["result"])))
                if not okv:
                    continue
                evaluable += 1
                tk = taker_net(cs["p"], cs["bid"], cs["ask"], cs["result"])
                if tk is not None:
                    taker_same.append((cs["event"], tk))
                if not filled:
                    continue
                v = net_pnl(side, lvl, cs["result"])
                fills.append((cs["event"], v))
                per_day[cs["day"]].append(v)
                sens["fee_m0"].append((cs["event"], net_pnl(side, lvl, cs["result"], m=0)))
                sens["no_penalty"].append((cs["event"], net_pnl(side, lvl, cs["result"], penalty=0)))
            st = cluster_boot(fills)
            out.append({
                "agent": agent, "category": AGENTS[agent]["category"], "variant": variant,
                "decisions": len(rows), "orders": orders, "evaluable_orders": evaluable,
                "fill_rate": (st["n"] / evaluable) if evaluable else None,
                "stats": st,
                "by_day": {d: {"n": len(v), "mean": sum(v) / len(v)} for d, v in sorted(per_day.items())},
                "sensitivity": {k: {"n": len(v), "mean": (sum(x for _, x in v) / len(v)) if v else None}
                                for k, v in sens.items()},
                "taker_rule_same_markets": {"n": len(taker_same),
                                            "mean": (sum(x for _, x in taker_same) / len(taker_same)) if taker_same else None},
            })
    verdicts(out)
    return out


def backtest_cases(pm_db: str) -> list[dict[str, Any]]:
    from .pm_models import load_rows, walk_forward
    c = sqlite3.connect(f"file:{pm_db}?mode=ro", uri=True, timeout=60)
    try:
        rows = load_rows(c)
    finally:
        c.close()
    cases = []
    for agent, g in AGENTS.items():
        for r, p in walk_forward(rows, g):
            snap = _ts(r["snap_at"])
            cases.append({"agent": agent, "ticker": r["ticker"], "event": r["event_ticker"] or r["ticker"],
                          "day": r["day"], "p": p, "bid": r["snap_bid"], "ask": r["snap_ask"], "result": int(r["result"]),
                          "t_place": math.floor(snap / 60) * 60 + 60,
                          "t_cancel": _ts(r["close_time"]) - CANCEL_BEFORE_CLOSE_S})
    return cases


def live_cases(main_db: str, since: str | None = None, until: str | None = None) -> list[dict[str, Any]]:
    """The agents' recorded live forecasts (fast_settle -> accuracy_forecasts) on settled markets."""
    c = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=60)
    try:
        mk = {t: (e, ct, 1 if res == "yes" else 0) for t, e, ct, res in c.execute(
            "SELECT ticker, event_ticker, close_time, result FROM fs_markets WHERE result IN ('yes','no')")}
        ph = ",".join("?" * len(AGENTS))
        q = (f"SELECT agent_id, market_id, recorded_at, predicted_probability, metadata_json FROM accuracy_forecasts"
             f" WHERE venue='kalshi' AND agent_id IN ({ph})")
        args: list[Any] = list(AGENTS)
        if since:
            q += " AND recorded_at >= ?"
            args.append(since)
        if until:
            q += " AND recorded_at < ?"
            args.append(until)
        rows = c.execute(q, args).fetchall()
    finally:
        c.close()
    cases = []
    for agent, t, at, p, mj in rows:
        if t not in mk:
            continue
        md = json.loads(mj or "{}")
        e, ct, res = mk[t]
        cases.append({"agent": agent, "ticker": t, "event": e or t, "day": ct[:10], "p": p,
                      "bid": md.get("bid"), "ask": md.get("ask"), "result": res,
                      "t_place": _ts(at) + PLACE_DELAY_LIVE_S, "t_cancel": _ts(ct) - CANCEL_BEFORE_CLOSE_S})
    return cases


def report(results: list[dict[str, Any]], **meta: Any) -> dict[str, Any]:
    return _round({"schema": "noema.kalshi_maker.v1", "mode": "paper/research only",
                   "prereg": "docs/research/kalshi-maker-prereg-2026-10-22.md", **meta,
                   "tests": len(results), "passes": [f"{r['agent']}/{r['variant']}" for r in results if r["verdict"] == "PASS"],
                   "promising": [f"{r['agent']}/{r['variant']}" for r in results if r["verdict"].startswith("promising")],
                   "results": results})


def run_backtest(pm_db: str, main_db: str, avenues_db: str) -> dict[str, Any]:
    cases = backtest_cases(pm_db)
    lcases = live_cases(main_db)
    ev = load_evidence(main_db, avenues_db, {c["ticker"] for c in cases} | {c["ticker"] for c in lcases})
    primary = simulate(cases, ev)
    live = simulate(lcases, ev)
    return {"primary": report(primary, dataset="pm_train walk-forward OOS snapshots", cases=len(cases)),
            "secondary_live_forecasts": report(live, dataset="recorded live forecasts (accuracy_forecasts)",
                                               cases=len(lcases))}


# ---------------------------------------------------------------- forward paper run
SCHEMA = """
CREATE TABLE IF NOT EXISTS km_trades (trade_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, ts REAL NOT NULL,
    yes_price REAL, count REAL, taker_side TEXT);
CREATE TABLE IF NOT EXISTS km_fetched (ticker TEXT PRIMARY KEY, at TEXT NOT NULL, n INTEGER);
CREATE TABLE IF NOT EXISTS km_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS km_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
    report_json TEXT NOT NULL);
"""


def _state(k: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = k.execute("SELECT v FROM km_state WHERE k=?", (key,)).fetchone()
    return r[0] if r else default


KM_PAGE, KM_MAX_PAGES = 1000, 20


def fetch_prints(k: sqlite3.Connection, tickers: list[str], client: Any, max_pages: int = KM_MAX_PAGES) -> int:
    """Public trade prints for settled markets the forward run quoted (read-only GET, once per ticker)."""
    n = 0
    for t in tickers:
        cursor, got = None, 0
        for _ in range(max_pages):
            params: dict[str, Any] = {"ticker": t, "limit": KM_PAGE}
            if cursor:
                params["cursor"] = cursor
            r = client.get(f"{KALSHI}/markets/trades", params=params)
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            j = r.json()
            rows = [(x["trade_id"], t, _ts(x["created_time"]), float(x["yes_price_dollars"]),
                     float(x.get("count_fp") or 0), x.get("taker_side")) for x in j.get("trades", [])]
            k.executemany("INSERT OR IGNORE INTO km_trades VALUES (?,?,?,?,?,?)", rows)
            got += len(rows)
            cursor = j.get("cursor")
            if not cursor or not rows:
                break
            time.sleep(0.15)
        k.execute("INSERT OR REPLACE INTO km_fetched VALUES (?,?,?)", (t, datetime.now(UTC).isoformat(), got))
        k.commit()
        n += got
        time.sleep(0.15)
    return n


def forward_once(main_db: str, maker_db: str, avenues_db: str | None, client: Any = None,
                 now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    k = sqlite3.connect(maker_db, timeout=30)
    try:
        k.executescript(SCHEMA)
        start = _state(k, "forward_start")
        done_rep = k.execute("SELECT report_json FROM km_reports WHERE kind='forward_final' ORDER BY id DESC LIMIT 1").fetchone()
        if done_rep:  # the 7-day run is over: keep the frozen final report, do no further work
            return json.loads(done_rep[0])
        if not start:
            start = now.isoformat()
            k.execute("INSERT INTO km_state VALUES ('forward_start', ?)", (start,))
            k.execute("INSERT OR REPLACE INTO km_state VALUES ('forward_end', ?)",
                      ((datetime.fromisoformat(start) + timedelta(days=FORWARD_DAYS)).isoformat(),))
            k.commit()
        end = _state(k, "forward_end") or start
        cases = live_cases(main_db, since=start, until=end)
        quoted = sorted({c["ticker"] for c in cases if any(decide(c["p"], c["bid"], c["ask"], v) for v in VARIANTS)})
        done = {r[0] for r in k.execute("SELECT ticker FROM km_fetched")}
        todo = [t for t in quoted if t not in done]
        fetched = 0
        if todo:
            import httpx
            cl = client or httpx.Client(timeout=20, headers={"accept": "application/json"})
            fetched = fetch_prints(k, todo[:200], cl)
        ev = load_evidence(main_db, None, {c["ticker"] for c in cases}, maker_db=maker_db)
        # every km_trades ticker has a complete print history (fetched after settlement)
        res = simulate(cases, ev)
        # forecasts stop at forward_end; the verdict freezes a day later so late markets settle and get prints
        final = now >= datetime.fromisoformat(end) + timedelta(hours=FINAL_GRACE_H)
        rep = report(res, dataset="forward paper run on live forecasts", window=[start, end], cases=len(cases),
                     markets_quoted=len(quoted), prints_fetched_this_cycle=fetched, final=final)
        k.execute("INSERT INTO km_reports (at, kind, report_json) VALUES (?,?,?)",
                  (now.isoformat(), "forward_final" if final else "forward", json.dumps(rep)))
        k.commit()
        return rep
    finally:
        k.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--pm-db", default="data/pm_train.db")
    ap.add_argument("--avenues-db", default="data/edge_avenues.db")
    ap.add_argument("--maker-db", default="data/kalshi_maker.db")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--loop", action="store_true", help="forward paper run (7 days from first start)")
    ap.add_argument("--interval", type=int, default=900)
    a = ap.parse_args()
    if a.backtest:
        rep = run_backtest(a.pm_db, a.db, a.avenues_db)
        txt = json.dumps(rep, indent=1)
        if a.out:
            with open(a.out, "w") as f:
                f.write(txt)
        print(json.dumps({k: {kk: rep[k][kk] for kk in ("cases", "tests", "passes", "promising")} for k in rep}))
        return
    from .heartbeats import beat
    while True:
        try:
            rep = forward_once(a.db, a.maker_db, a.avenues_db)
            beat(HB_NAME, True, cadence_s=a.interval)
            print(json.dumps({k: rep[k] for k in ("window", "cases", "markets_quoted", "passes", "promising", "final")}),
                  flush=True)
            if rep.get("final") and not a.loop:
                return
        except Exception as e:  # noqa: BLE001 - keep the loop alive, surface the error on the heartbeat
            beat(HB_NAME, False, f"{type(e).__name__}: {e}", cadence_s=a.interval)
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
        if not a.loop:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
