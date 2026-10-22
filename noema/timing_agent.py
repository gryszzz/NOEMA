"""TIMING / DAY-TRADER agent (PAPER ONLY — nothing here can place a real order or touch a flag).

After a candidate passes the screens, a timing agent decides WHEN to enter and exit on 1m candles:
  entry  : pullback to VWAP/EMA after a first impulse, or breakout with volume confirmation,
           order-flow imbalance confirmation, spike veto (no buying right after a vertical 5m move),
           liquidity/slippage check, limit-style entries (filled only if a later candle trades through
           the limit) and a no-trade filter (dead volume, gaps, extreme volatility, thin pool).
  manage : scale-out at tp1 with stop to break-even, optional scale-in on a confirmed hold, runner under
           a volatility trailing stop, hard stop, time stop; re-entry after a reset with a cooldown and a
           max trades/day per token.
  learn  : every decision is logged with its features and outcome path (data/timing.db). Timing genomes
           live in their own hire/fire roster (same rules as agent_lifecycle: fired when the 95% upper
           bound of mean net < 0 at n >= MIN_N, replaced by a mutation of the best survivor). Walk-forward
           evaluation (train on earlier tokens, score once on later ones) with Benjamini-Hochberg across
           every genome carried into a holdout. All returns are net of fees and pool slippage.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import random
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from . import edge_mining as em
from . import timing_candles as tc

FEE_SIDE = float(os.getenv("NOEMA_TIMING_FEE_SIDE", "0.01"))      # DEX fee + priority per side
NOTIONAL = float(os.getenv("NOEMA_TIMING_NOTIONAL", "25"))         # paper clip, drives slippage
MIN_N = int(os.getenv("NOEMA_LIFECYCLE_MIN_N", "30"))
POP = 8
MAX_WATCH = int(os.getenv("NOEMA_TIMING_MAX_WATCH", "20"))
CANDLE_REFRESH_S = float(os.getenv("NOEMA_TIMING_CANDLE_REFRESH_S", "120"))  # GeckoTerminal budget shared with the crawler
Candle = tuple  # (ts, o, h, l, c, v)


@dataclass
class Genome:
    mode: str = "pullback"        # pullback | breakout | either
    impulse_min: float = 0.08     # first impulse (high vs pass price) before a pullback counts
    pb_band: float = 0.02         # close within this of VWAP/EMA9
    brk_n: int = 10               # breakout over the N-bar high
    vol_mult: float = 2.0         # breakout volume vs median volume
    ofi_min: float = 0.1          # candle order-flow imbalance needed
    spike_max: float = 0.25       # veto entry if 5m return > this
    limit_off: float = 0.005      # limit below close
    window_min: int = 45          # how long after the pass to look for an entry
    min_vol5_usd: float = 500.0   # no-trade: dead tape
    max_atr: float = 0.12         # no-trade: per-minute ATR% too wild
    max_slip: float = 0.03        # no-trade: round-trip slippage at NOTIONAL
    tp1: float = 0.12
    tp1_sell: float = 0.5
    stop: float = -0.07
    trail_atr: float = 3.0
    min_trail: float = 0.04
    time_stop_min: int = 90
    scale_in: float = 0.0         # add this fraction after +scale_at hold (0 = off)
    scale_at: float = 0.05
    max_trades: int = 3
    cooldown_min: int = 10

    @classmethod
    def of(cls, d: dict[str, Any] | None) -> Genome:
        g = cls()
        for k, v in (d or {}).items():
            if hasattr(g, k):
                setattr(g, k, type(getattr(g, k))(v))
        return g


def mutate(g: Genome, rng: random.Random) -> Genome:
    d = asdict(g)
    for k, v in d.items():
        if k == "mode":
            if rng.random() < 0.2:
                d[k] = rng.choice(["pullback", "breakout", "either"])
        elif isinstance(v, int):
            d[k] = max(1, round(v * rng.uniform(0.75, 1.33)))
        elif v:
            d[k] = v * rng.uniform(0.75, 1.33)
        elif rng.random() < 0.2:
            d[k] = rng.uniform(0.0, 0.5) if k == "scale_in" else 0.0
    d["stop"] = -abs(d["stop"])
    d["tp1_sell"] = min(0.9, d["tp1_sell"])
    return Genome(**d)


# ----------------------------------------------------------------------------- features
def ema(xs: list[float], n: int) -> float:
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def features(cs: list[Candle], i: int, pass_px: float, liq: float) -> dict[str, float]:
    """Only candles [0..i] (closed) are used: no lookahead."""
    w = cs[max(0, i - 29): i + 1]
    pv = sum((c[2] + c[3] + c[4]) / 3 * c[5] for c in w)
    vol = sum(c[5] for c in w)
    vwap = pv / vol if vol > 0 else cs[i][4]
    closes = [c[4] for c in cs[max(0, i - 30): i + 1]]
    e9 = ema(closes, 9)
    l5 = cs[max(0, i - 4): i + 1]
    r5 = cs[i][4] / l5[0][1] - 1 if l5[0][1] > 0 else 0.0
    ofi = sum(((c[4] - c[1]) / (c[2] - c[3])) * c[5] for c in l5 if c[2] > c[3]) / max(sum(c[5] for c in l5), 1e-9)
    atr = sum((c[2] - c[3]) / c[4] for c in w[-14:] if c[4] > 0) / max(len(w[-14:]), 1)
    prior = cs[max(0, i - 10): i]
    hi_n = max((c[2] for c in prior), default=cs[i][2])
    vols = sorted(c[5] for c in w)
    med = vols[len(vols) // 2] if vols else 0.0
    gap = max((b[0] - a[0] for a, b in itertools.pairwise(l5)), default=60)
    return {"close": cs[i][4], "vwap": vwap, "ema9": e9, "r5": r5, "ofi": ofi, "atr": atr, "hi_n": hi_n,
            "vol_ratio": cs[i][5] / med if med > 0 else 0.0, "vol5_usd": sum(c[5] for c in l5),
            "impulse": max(c[2] for c in cs[: i + 1]) / pass_px - 1 if pass_px > 0 else 0.0,
            "slip": 2 * NOTIONAL / max(liq, 1.0), "gap_s": gap}


def no_trade(f: dict[str, float], g: Genome) -> str | None:
    if f["slip"] > g.max_slip:
        return "pool too thin (slippage)"
    if f["vol5_usd"] < g.min_vol5_usd:
        return "dead tape"
    if f["atr"] > g.max_atr:
        return "too wild"
    if f["gap_s"] > 180:
        return "candle gaps"
    return None


def trigger(f: dict[str, float], g: Genome, brk_hi: float | None = None) -> tuple[str | None, float]:
    """Return (signal, limit price) or (None, 0)."""
    if f["r5"] > g.spike_max:
        return None, 0.0
    if f["ofi"] < g.ofi_min:
        return None, 0.0
    c = f["close"]
    if g.mode in ("pullback", "either") and f["impulse"] >= g.impulse_min:
        anchor = max(f["vwap"], f["ema9"])
        if abs(c / anchor - 1) <= g.pb_band:
            return "pullback", c * (1 - g.limit_off)
    if g.mode in ("breakout", "either") and c > f["hi_n"] and f["vol_ratio"] >= g.vol_mult:
        return "breakout", c * (1 + g.limit_off)  # marketable limit, capped chase
    return None, 0.0


# ----------------------------------------------------------------------------- simulation
def _leg_cost(liq: float) -> float:
    return 2 * FEE_SIDE + min(2 * NOTIONAL / max(liq, 1.0), 1.0)


def manage(cs: list[Candle], j: int, entry: float, g: Genome) -> tuple[int, float, str, list[dict]]:
    """Walk candles from j (fill bar). Returns (exit index, gross return on 1.0 unit incl. scale-in, reason, path)."""
    rem, tp_hit, peak, added = 1.0, False, entry, 0.0
    cost_basis, realized, rets, last = entry, 0.0, [], entry
    path = []
    for k in range(j, len(cs)):
        ts, o, h, lo, c, _ = cs[k]
        mins = (ts - cs[j][0]) / 60
        rets.append(abs(c / last - 1) if last > 0 else 0)
        last = c
        stop_px = cost_basis if tp_hit else entry * (1 + g.stop)
        if lo <= stop_px:                                            # stop touched intrabar (gap-safe: fill at min(open, stop))
            realized += rem * (min(o, stop_px) / cost_basis - 1)
            return k, realized, "break-even stop" if tp_hit else "hard stop", path
        peak = max(peak, h)
        if not tp_hit and h >= entry * (1 + g.tp1):
            sell = g.tp1_sell * rem
            realized += sell * g.tp1
            rem -= sell
            tp_hit = True
            path.append({"m": round(mins, 1), "act": "tp1"})
        if g.scale_in > 0 and not added and not tp_hit and c >= entry * (1 + g.scale_at) and mins >= 3:
            nb = (rem * cost_basis + g.scale_in * c) / (rem + g.scale_in)  # averaged-up basis on more size
            rem += g.scale_in
            cost_basis, added = nb, g.scale_in
            path.append({"m": round(mins, 1), "act": "scale_in"})
        if tp_hit:
            atr = sum(rets[-14:]) / len(rets[-14:])
            trail = max(g.min_trail, min(0.4, g.trail_atr * atr))
            if c <= peak * (1 - trail):
                realized += rem * (c / cost_basis - 1)
                return k, realized, "trailing stop", path
        if mins >= g.time_stop_min:
            realized += rem * (c / cost_basis - 1)
            return k, realized, "time stop", path
    realized += rem * (cs[-1][4] / cost_basis - 1)
    return len(cs) - 1, realized, "data end", path


def run_token(cs: list[Candle], t0: int, pass_px: float, liq: float, g: Genome,
              log: list[dict] | None = None) -> dict[str, Any]:
    """Policy on one passed candidate. Returns per-token result (net is the sum of legs, unit size each)."""
    start = next((i for i, c in enumerate(cs) if c[0] >= t0), None)
    if start is None or start < 5:
        return {"traded": False, "net": 0.0, "legs": [], "skip": "no candles"}
    legs, i, last_exit, pend, skip = [], start, -10 ** 9, None, None
    end_entry = t0 + g.window_min * 60
    while i < len(cs) - 1 and cs[i][0] <= end_entry and len(legs) < g.max_trades:
        if pend:                                                  # limit order resting for this bar
            sig, lim, f = pend
            o, _h, lo = cs[i][1], cs[i][2], cs[i][3]
            fill = None
            if sig == "pullback" and lo <= lim:
                fill = min(o, lim)
            elif sig == "breakout" and o <= lim:
                fill = o
            pend = None
            if fill:
                k, gross, why, path = manage(cs, i, fill, g)
                net = gross - _leg_cost(liq) * (1 + (0.5 if "scale_in" in str(path) else 0))
                legs.append({"sig": sig, "in_m": round((cs[i][0] - t0) / 60, 1), "out_m": round((cs[k][0] - t0) / 60, 1),
                             "gross": gross, "net": net, "why": why})
                if log is not None:
                    log.append({"sig": sig, "features": f, "gross": gross, "net": net, "why": why, "path": path})
                last_exit, i = cs[k][0], k + 1
                continue
        if cs[i][0] - last_exit >= g.cooldown_min * 60:
            f = features(cs, i, pass_px, liq)
            skip = no_trade(f, g)
            if not skip:
                sig, lim = trigger(f, g)
                if sig:
                    pend = (sig, lim, f)
        i += 1
    return {"traded": bool(legs), "net": sum(x["net"] for x in legs), "legs": legs, "skip": None if legs else (skip or "no setup")}


def enter_on_pass(cs: list[Candle], t0: int, liq: float, g: Genome | None = None) -> dict[str, Any] | None:
    """Current behaviour baseline: buy the next open after the pass, same intraday exits."""
    g = g or Genome()
    j = next((i for i, c in enumerate(cs) if c[0] >= t0), None)
    if j is None or j >= len(cs) - 1:
        return None
    _, gross, why, _ = manage(cs, j, cs[j][1], g)
    return {"net": gross - _leg_cost(liq), "gross": gross, "why": why}


# ----------------------------------------------------------------------------- data + evaluation
def load_samples(db: str, timing_db: str) -> list[dict[str, Any]]:
    src = em._ro(db)
    cc = tc.cache(timing_db)
    try:
        rows = src.execute("SELECT token_mint, captured_at, reference_price_usd, reference_liquidity_usd FROM trench_candidates"
                           " WHERE disposition='observe' ORDER BY captured_at").fetchall()
        if src.execute("SELECT 1 FROM sqlite_master WHERE name='alpha_candidates'").fetchone():
            rows = list(rows) + src.execute("SELECT address, evaluated_at, price, liquidity FROM alpha_candidates"
                                            " WHERE passed=1 AND chain='solana' ORDER BY evaluated_at").fetchall()
        out, seen = [], set()
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            p = cc.execute("SELECT pool FROM pools WHERE mint=?", (r[0],)).fetchone()
            if not p or not p[0]:
                continue
            t0 = int(datetime.fromisoformat(r[1]).timestamp())
            cs = tc.load(cc, p[0], t0 - 3600, t0 + 3 * 3600)
            if len(cs) < 10:
                continue
            out.append({"mint": r[0], "t0": t0, "px": r[2] or cs[0][4],
                        "liq": r[3] or 0.0, "cs": cs})
        return out
    finally:
        src.close()
        cc.close()


def stats(xs: list[float]) -> dict[str, Any]:
    lo, hi, p = em.bootstrap(xs)
    return {"n": len(xs), "mean": round(em.mean(xs), 4), "lo95": None if math.isnan(lo) else round(lo, 4),
            "hi95": None if math.isnan(hi) else round(hi, 4), "p": round(p, 4),
            "win_rate": round(sum(x > 0 for x in xs) / len(xs), 3) if xs else None}


def genome_pool(rng: random.Random, k: int = 24) -> list[Genome]:
    base = [Genome(), Genome(mode="breakout"), Genome(mode="either"), Genome(scale_in=0.5),
            Genome(tp1=0.25, stop=-0.12, time_stop_min=150), Genome(mode="either", ofi_min=0.3, spike_max=0.15)]
    while len(base) < k:
        base.append(mutate(rng.choice(base[:6]), rng))
    return base


def walk_forward(samples: list[dict[str, Any]], folds: int = 4, seed: int = 5) -> dict[str, Any]:
    """Chronological folds: pick the best genome on all earlier folds, score it once on the next fold.
    Every genome is also scored on every holdout, and BH is applied across them (multiple-testing)."""
    rng = random.Random(seed)
    pool = genome_pool(rng)
    samples = sorted(samples, key=lambda s: s["t0"])
    cut = [len(samples) * k // (folds + 1) for k in range(folds + 2)]
    oos_sel, oos_base, oos_pair, chosen = [], [], [], []
    for k in range(1, folds + 1):
        train, test = samples[: cut[k]], samples[cut[k]: cut[k + 1]]
        if not train or not test:
            continue
        best = max(pool, key=lambda g: em.mean([run_token(s["cs"], s["t0"], s["px"], s["liq"], g)["net"] for s in train]))
        chosen.append(asdict(best))
        for s in test:
            b = enter_on_pass(s["cs"], s["t0"], s["liq"])
            if b is None:
                continue
            t = run_token(s["cs"], s["t0"], s["px"], s["liq"], best)
            oos_sel.append(t["net"])
            oos_base.append(b["net"])
            oos_pair.append(t["net"] - b["net"])
    # all-genome holdout (last 40%) with BH
    split = int(len(samples) * 0.6)
    hold = samples[split:]
    rows = []
    for g in pool:
        xs = [run_token(s["cs"], s["t0"], s["px"], s["liq"], g)["net"] for s in hold]
        traded = [x for x in xs if x != 0]
        rows.append({"genome": asdict(g), "all": stats(xs), "traded": stats(traded) if len(traded) > 1 else {"n": len(traded)}})
    adj = em.bh_adjust([r["all"]["p"] for r in rows])
    for r, a in zip(rows, adj):
        r["p_bh"] = round(a, 4)
        r["survives"] = bool(r["all"]["n"] >= MIN_N and (r["all"]["lo95"] or -1) > 0 and a < 0.05)
    rows.sort(key=lambda r: -r["all"]["mean"])
    return {"n_tokens": len(samples), "folds": folds, "genomes_tested": len(pool),
            "oos_timing_selected": stats(oos_sel), "oos_enter_on_pass": stats(oos_base),
            "oos_paired_diff": stats(oos_pair), "trade_rate_oos": round(sum(x != 0 for x in oos_sel) / max(len(oos_sel), 1), 3),
            "chosen": chosen[-1:] , "holdout_top": rows[:5], "survivors": sum(r["survives"] for r in rows),
            "costs": {"fee_side": FEE_SIDE, "notional_usd": NOTIONAL, "slippage": "2*notional/liquidity per round trip"}}


# ----------------------------------------------------------------------------- roster + live paper loop
SCHEMA = """
CREATE TABLE IF NOT EXISTS t_agents (agent_id TEXT PRIMARY KEY, name TEXT, generation INTEGER, parent_id TEXT,
  status TEXT, genome_json TEXT, hired_at REAL, fired_at REAL, fire_reason TEXT, stats_json TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS t_watch (mint TEXT, pool TEXT, t0 INTEGER, px REAL, liq REAL, PRIMARY KEY (mint));
CREATE TABLE IF NOT EXISTS t_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, agent_id TEXT, mint TEXT,
  action TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS t_results (agent_id TEXT, mint TEXT, t0 INTEGER, scored_at REAL, net REAL, legs_json TEXT,
  PRIMARY KEY (agent_id, mint));
CREATE TABLE IF NOT EXISTS t_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, report_json TEXT);
CREATE TABLE IF NOT EXISTS t_events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, agent_id TEXT, event TEXT, detail TEXT);
"""


def connect(path: str) -> sqlite3.Connection:
    c = tc.cache(path)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def hire(c: sqlite3.Connection, g: Genome, gen: int, parent: str | None, origin: str) -> str:
    aid = hashlib.sha1(f"{asdict(g)}{time.time()}{random.random()}".encode()).hexdigest()[:10]
    c.execute("INSERT INTO t_agents (agent_id,name,generation,parent_id,status,genome_json,hired_at) VALUES (?,?,?,?,?,?,?)",
              (aid, f"TIME-{g.mode[:4].upper()}-g{gen}-{aid[:4]}", gen, parent, "shadow", json.dumps(asdict(g)), time.time()))
    c.execute("INSERT INTO t_events (at,agent_id,event,detail) VALUES (?,?,?,?)", (time.time(), aid, "hired", origin))
    return aid


def ensure_roster(c: sqlite3.Connection, rng: random.Random) -> None:
    n = c.execute("SELECT COUNT(*) FROM t_agents WHERE status!='fired'").fetchone()[0]
    for g in genome_pool(rng, POP)[n:POP]:
        hire(c, g, 0, None, "seed")
    c.commit()


def lifecycle(c: sqlite3.Connection, rng: random.Random) -> None:
    """Same rules as agent_lifecycle: prospective scores only; fire on hi95<0 at n>=MIN_N or mean<0 at n>=3*MIN_N."""
    live = c.execute("SELECT * FROM t_agents WHERE status IN ('shadow','earning')").fetchall()
    for a in live:
        xs = [r[0] for r in c.execute("SELECT net FROM t_results WHERE agent_id=?", (a["agent_id"],))]
        s = stats(xs) if len(xs) > 1 else {"n": len(xs)}
        fire = None
        if len(xs) >= MIN_N and (s.get("hi95") is not None and s["hi95"] < 0):
            fire = f"losing after costs (hi95 {s['hi95']:+.3f}, n={len(xs)})"
        elif len(xs) >= 3 * MIN_N and s["mean"] < 0:
            fire = f"mean net {s['mean']:+.3f} after n={len(xs)}"
        status = "earning" if len(xs) >= MIN_N and (s.get("lo95") or -1) > 0 else "shadow"
        if fire:
            c.execute("UPDATE t_agents SET status='fired', fired_at=?, fire_reason=?, stats_json=? WHERE agent_id=?",
                      (time.time(), fire, json.dumps(s), a["agent_id"]))
            c.execute("INSERT INTO t_events (at,agent_id,event,detail) VALUES (?,?,?,?)", (time.time(), a["agent_id"], "fired", fire))
        else:
            c.execute("UPDATE t_agents SET status=?, stats_json=? WHERE agent_id=?", (status, json.dumps(s), a["agent_id"]))
    surv = c.execute("SELECT * FROM t_agents WHERE status IN ('shadow','earning')").fetchall()
    while len(surv) < POP:
        best = max(surv, key=lambda r: json.loads(r["stats_json"] or "{}").get("mean", -1), default=None)
        g = mutate(Genome.of(json.loads(best["genome_json"])) if best else Genome(), rng)
        hire(c, g, (best["generation"] + 1) if best else 0, best["agent_id"] if best else None, "replacement")
        surv = c.execute("SELECT * FROM t_agents WHERE status IN ('shadow','earning')").fetchall()
    c.commit()


def watch_new(c: sqlite3.Connection, db: str, max_age_s: int = 1800) -> int:
    """Pull freshly passed candidates (trench 'observe' + alpha screen passes) onto the watchlist."""
    src = em._ro(db)
    n = 0
    try:
        since = datetime.fromtimestamp(time.time() - max_age_s, UTC).isoformat()
        rows = src.execute("SELECT token_mint, captured_at, reference_price_usd, reference_liquidity_usd FROM trench_candidates"
                           " WHERE disposition='observe' AND captured_at>? ORDER BY opportunity_score DESC LIMIT 6",
                           (since,)).fetchall()
        if src.execute("SELECT 1 FROM sqlite_master WHERE name='alpha_candidates'").fetchone():
            rows += src.execute("SELECT address, evaluated_at, price, liquidity FROM alpha_candidates WHERE passed=1"
                                " AND chain='solana' AND evaluated_at>? ORDER BY score DESC LIMIT 6", (since,)).fetchall()
        for r in rows:
            if c.execute("SELECT COUNT(*) FROM t_watch").fetchone()[0] >= MAX_WATCH:
                break                                               # keeps candle refresh within budget
            if c.execute("SELECT 1 FROM t_watch WHERE mint=?", (r[0],)).fetchone():
                continue
            pool, res = tc.top_pool(c, r[0])
            if pool:
                c.execute("INSERT OR IGNORE INTO t_watch VALUES (?,?,?,?,?)",
                          (r[0], pool, int(datetime.fromisoformat(r[1]).timestamp()), r[2] or 0, r[3] or res))
                n += 1
    finally:
        src.close()
    c.commit()
    return n


def tick(c: sqlite3.Connection, now: float) -> dict[str, int]:
    """Refresh candles for active candidates and let every agent decide; score once the window + hold is over."""
    agents = c.execute("SELECT agent_id, genome_json FROM t_agents WHERE status IN ('shadow','earning')").fetchall()
    horizon = 3 * 3600
    done = scored = 0
    for w in c.execute("SELECT * FROM t_watch").fetchall():
        if now - w["t0"] > horizon + 600:
            cs = tc.load(c, w["pool"], w["t0"] - 3600, w["t0"] + horizon)
            for a in agents:
                if c.execute("SELECT 1 FROM t_results WHERE agent_id=? AND mint=?", (a["agent_id"], w["mint"])).fetchone():
                    continue
                if w["t0"] < c.execute("SELECT hired_at FROM t_agents WHERE agent_id=?", (a["agent_id"],)).fetchone()[0]:
                    continue                                            # prospective only
                log: list[dict] = []
                r = run_token(cs, w["t0"], w["px"], w["liq"], Genome.of(json.loads(a["genome_json"])), log)
                c.execute("INSERT INTO t_results VALUES (?,?,?,?,?,?)", (a["agent_id"], w["mint"], w["t0"], now, r["net"], json.dumps(r["legs"])))
                c.execute("INSERT INTO t_decisions (at,agent_id,mint,action,detail_json) VALUES (?,?,?,?,?)",
                          (now, a["agent_id"], w["mint"], "traded" if r["traded"] else "no_trade",
                           json.dumps({"skip": r["skip"], "legs": r["legs"], "log": log}, default=float)[:20000]))
                scored += 1
            c.execute("DELETE FROM t_watch WHERE mint=?", (w["mint"],))
            done += 1
            continue
        last = c.execute("SELECT MAX(ts) FROM candles WHERE pool=?", (w["pool"],)).fetchone()[0] or 0
        if now - last >= CANDLE_REFRESH_S:                          # 1m paths stored for every passed candidate
            tc.minute_candles(c, w["pool"], int(now), 60 if last else 300)
        cs = tc.load(c, w["pool"], w["t0"] - 3600, int(now))
        for a in agents:                                           # live intent log (what it would do right now)
            g = Genome.of(json.loads(a["genome_json"]))
            if not cs or now - w["t0"] > g.window_min * 60:
                continue
            f = features(cs, len(cs) - 1, w["px"], w["liq"])
            why = no_trade(f, g)
            sig = None if why else trigger(f, g)[0]
            c.execute("INSERT INTO t_decisions (at,agent_id,mint,action,detail_json) VALUES (?,?,?,?,?)",
                      (now, a["agent_id"], w["mint"], f"intent:{sig}" if sig else "wait",
                       json.dumps({"why": why, "f": {k: round(v, 5) for k, v in f.items()}})))
    c.execute("DELETE FROM t_decisions WHERE (action LIKE 'intent:%' OR action='wait') AND at<?", (now - 6 * 3600,))
    c.commit()
    return {"finished": done, "scored": scored}


def loop(db: str, timing_db: str, interval: float = 10.0) -> None:
    rng = random.Random()
    c = connect(timing_db)
    ensure_roster(c, rng)
    last_life = last_rep = 0.0
    while True:
        now = time.time()
        try:
            watch_new(c, db)
            tick(c, now)
            if now - last_life > 600:
                lifecycle(c, rng)
                last_life = now
            if now - last_rep > 6 * 3600:
                rep = walk_forward(load_samples(db, timing_db))
                c.execute("INSERT INTO t_reports (at, report_json) VALUES (?,?)", (now, json.dumps(rep)))
                c.execute("DELETE FROM t_reports WHERE id NOT IN (SELECT id FROM t_reports ORDER BY id DESC LIMIT 20)")
                c.commit()
                last_rep = now
        except Exception as e:  # noqa: BLE001 - keep the paper loop alive, log and continue
            print(datetime.now(UTC).isoformat(), "timing tick error:", repr(e), flush=True)
        time.sleep(max(1.0, interval - (time.time() - now)))


def board(timing_db: str) -> dict[str, Any]:
    """Read-only snapshot for /api/timing."""
    out: dict[str, Any] = {"schema": "tev.timing.v1", "paper_only": True, "agents": [], "watch": [], "recent": [], "report": None}
    if not os.path.exists(timing_db):
        return out
    c = sqlite3.connect(f"file:{timing_db}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='t_agents'").fetchone():
            return out
        for r in c.execute("SELECT * FROM t_agents ORDER BY status='fired', generation DESC LIMIT 40"):
            out["agents"].append({"id": r["agent_id"], "name": r["name"], "status": r["status"], "generation": r["generation"],
                                  "parent": r["parent_id"], "genome": json.loads(r["genome_json"]),
                                  "stats": json.loads(r["stats_json"] or "{}"), "fire_reason": r["fire_reason"]})
        out["watch"] = [dict(r) for r in c.execute("SELECT mint, pool, t0, liq FROM t_watch ORDER BY t0 DESC LIMIT 20")]
        out["recent"] = [{"at": r["at"], "agent": r["agent_id"], "mint": r["mint"], "action": r["action"]}
                         for r in c.execute("SELECT at, agent_id, mint, action FROM t_decisions ORDER BY id DESC LIMIT 30")]
        for a in out["agents"]:
            r = c.execute("SELECT COUNT(*), COALESCE(SUM(net),0) FROM t_results WHERE agent_id=?", (a["id"],)).fetchone()
            a["scored"], a["paper_pnl_usd"] = r[0], round(r[1] * NOTIONAL, 2)
        for w in out["watch"]:
            acts = [x[0] for x in c.execute("SELECT action FROM t_decisions WHERE mint=? AND at>? ORDER BY id DESC LIMIT ?",
                                            (w["mint"], time.time() - 120, POP))]
            enter = sum(a.startswith("intent:") for a in acts)
            w["state"] = "enter" if enter else ("wait" if acts else "idle")
            w["agents_entering"] = enter
        out["exited"] = [{"mint": r[0], "net_usd": round(r[1] * NOTIONAL, 2), "traded": r[2], "at": r[3]} for r in c.execute(
            "SELECT mint, AVG(net), SUM(legs_json!='[]'), MAX(scored_at) FROM t_results GROUP BY mint ORDER BY MAX(scored_at) DESC LIMIT 8")]
        out["paper_pnl_usd"] = round((c.execute("SELECT COALESCE(SUM(net),0) FROM t_results").fetchone()[0]) * NOTIONAL, 2)
        out["events"] = [dict(r) for r in c.execute("SELECT at, agent_id, event, detail FROM t_events ORDER BY id DESC LIMIT 20")]
        rep = c.execute("SELECT at, report_json FROM t_reports ORDER BY id DESC LIMIT 1").fetchone()
        if rep:
            out["report"] = {"at": rep["at"], **json.loads(rep["report_json"])}
        out["counts"] = {s: sum(a["status"] == s for a in out["agents"]) for s in ("shadow", "earning", "fired")}
    finally:
        c.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="NOEMA timing agent (paper only)")
    ap.add_argument("--db", default="data/noema.db")
    ap.add_argument("--timing-db", default="data/timing.db")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=float(os.getenv("NOEMA_TIMING_INTERVAL_S", "10")))
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    if a.loop:
        loop(a.db, a.timing_db, a.interval)
    elif a.backtest:
        rep = walk_forward(load_samples(a.db, a.timing_db))
        c = connect(a.timing_db)
        c.execute("INSERT INTO t_reports (at, report_json) VALUES (?,?)", (time.time(), json.dumps(rep)))
        c.commit()
        print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
