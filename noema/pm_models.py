"""Per-category forecasters for short-dated Kalshi markets, trained on pm_history's set.

Models (each yields P(YES) or None):
  market      quote mid at snapshot (the benchmark every agent must beat)
  spotvol     lognormal from the reference price with realized 5m vol and an
              hour-of-day seasonality profile built only from bars before the snapshot
              (crypto, indexes, FX, commodities)
  weather     archived Open-Meteo day-ahead forecast vs strike, normal error
Calibration/stacking per category (pure-python ridge logistic regression):
  market_cal  Platt on logit(mid)
  model_cal   Platt on logit(model)
  stack       logistic blend of logit(mid) and logit(model)
Walk-forward by day (fit on days strictly before the test day), clustered bootstrap
CIs by event, Benjamini-Hochberg across every (category, model) test, and a
fee-aware paper-trade backtest (Kalshi taker fee 0.07*P*(1-P) per contract).
Agents are genomes {category, model, window_days, ridge}; ones significantly worse
than the market are fired and replaced by a mutation of the best survivor.
Paper/forecast only.
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

from .pm_history import reference_symbol, weather_site

EPS = 0.01
MODELS = ("market", "market_cal", "spotvol", "weather", "model_cal", "stack")
WEATHER_SD_F = 3.0


def clip(p: float) -> float:
    return min(1 - EPS, max(EPS, p))


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def kalshi_fee(price: float) -> float:
    """Kalshi taker fee per contract (0.07 * P * (1-P)); orders round up to the cent."""
    return 0.07 * price * (1 - price)


# ---------------- reference-price model ----------------

class Bars:
    def __init__(self, rows: list[tuple[int, float]]) -> None:
        rows = sorted(rows)
        self.ts = [t for t, _ in rows]
        self.px = [p for _, p in rows]
        self.ret = [0.0] + [math.log(b / a) if a > 0 and b > 0 else 0.0 for a, b in zip(self.px, self.px[1:], strict=False)]

    # Yahoo bars are stamped at their START; a bar's close is only known at ts + BAR_S.
    BAR_S = 300

    def spot(self, at: int) -> tuple[float, int] | None:
        i = bisect.bisect_right(self.ts, at - self.BAR_S) - 1
        return (self.px[i], self.ts[i]) if i >= 0 and at - self.ts[i] < 3600 else None

    def vol_profile(self, at: int) -> tuple[float, list[float]] | None:
        """Per-5m vol over the prior 24h and an hour-of-day seasonality profile from the prior 30 days."""
        i = bisect.bisect_right(self.ts, at - self.BAR_S)
        j = bisect.bisect_left(self.ts, at - 86400)
        k = bisect.bisect_left(self.ts, at - 30 * 86400)
        recent = [r for r in self.ret[j:i] if r != 0.0]
        if len(recent) < 30:
            return None
        base = math.sqrt(sum(r * r for r in recent) / len(recent))
        by_h: dict[int, list[float]] = defaultdict(list)
        for t, r in zip(self.ts[k:i], self.ret[k:i], strict=False):
            if r != 0.0:
                by_h[datetime.fromtimestamp(t, UTC).hour].append(r * r)
        allv = [v for vs in by_h.values() for v in vs]
        mean_all = sum(allv) / len(allv) if allv else base * base
        prof = [math.sqrt((sum(by_h[h]) / len(by_h[h])) / mean_all) if len(by_h[h]) >= 20 and mean_all > 0 else 1.0
                for h in range(24)]
        # normalise the profile so the last 24h level is preserved
        return base, prof


def spotvol_prob(bars: Bars, snap: int, close: int, strike_type: str, lo: float | None, hi: float | None) -> float | None:
    s = bars.spot(snap)
    vp = bars.vol_profile(snap)
    if not s or not vp or close <= snap:
        return None
    spot, _ = s
    base, prof = vp
    var, t = 0.0, snap
    while t < close:
        step = min(300, close - t)
        var += (base * prof[datetime.fromtimestamp(t, UTC).hour]) ** 2 * step / 300
        t += step
    sd = max(math.sqrt(var) * 1.1, 1e-6)

    def above(k: float) -> float:
        return phi(math.log(spot / k) / sd) if k and k > 0 else 1.0
    if strike_type in ("greater", "greater_or_equal") and lo:
        return clip(above(lo))
    if strike_type in ("less", "less_or_equal") and hi:
        return clip(1 - above(hi))
    if strike_type == "between" and lo and hi:
        return clip(above(lo) - above(hi))
    return None


_MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def event_day(event_ticker: str) -> str | None:
    try:
        part = event_ticker.split("-")[1]
        return f"20{part[:2]}-{_MONTHS[part[2:5]]:02d}-{int(part[5:7]):02d}"
    except (IndexError, KeyError, ValueError):
        return None


def weather_prob(fc: float, strike_type: str, lo: float | None, hi: float | None) -> float | None:
    def ge(x: float) -> float:
        return 1 - phi((x - fc) / WEATHER_SD_F)
    if strike_type in ("greater", "greater_or_equal") and lo is not None:
        return clip(ge(lo + 0.5))
    if strike_type in ("less", "less_or_equal") and hi is not None:
        return clip(1 - ge(hi - 0.5))
    if strike_type == "between" and lo is not None and hi is not None:
        return clip(ge(lo - 0.5) - ge(hi + 0.5))
    return None


class FeatureSource:
    """Fundamental model probability for a market row (training or live)."""

    def __init__(self, c: sqlite3.Connection) -> None:
        self.bars: dict[str, Bars] = {}
        for sym in [r[0] for r in c.execute("SELECT DISTINCT symbol FROM pm_ref_bars")]:
            self.bars[sym] = Bars(c.execute("SELECT ts, close FROM pm_ref_bars WHERE symbol=?", (sym,)).fetchall())
        self.wx = {(s, d): (hi, lo) for s, d, hi, lo in c.execute("SELECT site, day, tmax, tmin FROM pm_weather_fc")}

    def model(self, r: dict[str, Any]) -> tuple[str, float] | None:
        snap = int(datetime.fromisoformat(r["snap_at"]).timestamp())
        close = int(datetime.fromisoformat(r["close_time"]).timestamp())
        sym = reference_symbol(r["series"])
        if sym and sym in self.bars:
            p = spotvol_prob(self.bars[sym], snap, close, r["strike_type"] or "", r["floor_strike"], r["cap_strike"])
            return ("spotvol", p) if p is not None else None
        site = weather_site(r["series"])
        if site:
            kind, code, _ = site
            day = event_day(r.get("event_ticker") or "")
            fc = self.wx.get((code, day)) if day else None
            if fc and fc[0 if kind == "high" else 1] is not None:
                p = weather_prob(fc[0 if kind == "high" else 1], r["strike_type"] or "", r["floor_strike"], r["cap_strike"])
                return ("weather", p) if p is not None else None
        return None


# ---------------- ridge logistic regression (Newton) ----------------

def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        if abs(m[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for k in range(col, n + 1):
                    m[r][k] -= f * m[col][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_logistic(xs: list[list[float]], ys: list[int], ridge: float = 1.0, prior: list[float] | None = None) -> list[float]:
    """Ridge toward `prior` (default: identity on the first logit feature, i.e. 'trust the input')."""
    d = len(xs[0])
    w = prior[:] if prior else [0.0] + [1.0] + [0.0] * (d - 2)
    pr = w[:]
    for _ in range(25):
        g = [ridge * (w[j] - pr[j]) for j in range(d)]
        h = [[ridge if i == j else 0.0 for j in range(d)] for i in range(d)]
        for x, y in zip(xs, ys, strict=False):
            p = sigmoid(sum(wi * xi for wi, xi in zip(w, x, strict=False)))
            e, s = p - y, p * (1 - p)
            for i in range(d):
                g[i] += e * x[i]
                for j in range(d):
                    h[i][j] += s * x[i] * x[j]
        step = _solve(h, g)
        w = [wi - si for wi, si in zip(w, step, strict=False)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def design(model: str, mid: float, mp: float | None) -> list[float] | None:
    if model == "market_cal":
        return [1.0, logit(mid)]
    if model == "model_cal":
        return None if mp is None else [1.0, logit(mp)]
    if model == "stack":
        return None if mp is None else [1.0, logit(mid), logit(mp)]
    return None


def stack_prior(model: str) -> list[float]:
    return {"market_cal": [0.0, 1.0], "model_cal": [0.0, 1.0], "stack": [0.0, 1.0, 0.0]}[model]


# ---------------- data, evaluation, leaderboard ----------------

def load_rows(c: sqlite3.Connection) -> list[dict[str, Any]]:
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM pm_train WHERE snap_mid IS NOT NULL AND snap_bid IS NOT NULL AND snap_ask - snap_bid <= 0.2"
        " ORDER BY close_time")]
    c.row_factory = None
    fs = FeatureSource(c)
    for r in rows:
        m = fs.model(r)
        r["model_kind"], r["model_p"] = m if m else (None, None)
        r["day"] = r["close_time"][:10]
    return rows


def brier(p: float, y: int) -> float:
    return (p - y) ** 2


def logloss(p: float, y: int) -> float:
    p = clip(p)
    return -math.log(p if y else 1 - p)


def cluster_ci(vals: list[tuple[str, float]], n_boot: int = 1000, seed: int = 7) -> tuple[float, float, float, float]:
    """Mean, 95% CI and two-sided bootstrap p-value, resampling whole events (strikes are correlated)."""
    by: dict[str, list[float]] = defaultdict(list)
    for k, v in vals:
        by[k].append(v)
    keys = list(by)
    sums = {k: (sum(v), len(v)) for k, v in by.items()}
    tot = sum(s for s, _ in sums.values())
    n = sum(m for _, m in sums.values())
    mean = tot / n if n else 0.0
    if len(keys) < 5:
        return mean, -math.inf, math.inf, 1.0
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        s = m = 0.0
        for _ in keys:
            a, b = sums[keys[rng.randrange(len(keys))]]
            s += a
            m += b
        boots.append(s / m)
    boots.sort()
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]
    p = 2 * min(sum(b <= 0 for b in boots), sum(b >= 0 for b in boots)) / n_boot
    return mean, lo, hi, max(p, 1 / n_boot)


def bh(pvals: list[float], q: float = 0.05) -> list[bool]:
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    m, cut = len(pvals), -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            cut = rank
    passed = set(order[:cut]) if cut > 0 else set()
    return [i in passed for i in range(m)]


def predict(model: str, w: list[float] | None, r: dict[str, Any]) -> float | None:
    mid, mp = r["snap_mid"], r["model_p"]
    if model == "market":
        return mid
    if model in ("spotvol", "weather"):
        return mp if r["model_kind"] == model else None
    x = design(model, mid, mp)
    return None if x is None or w is None else clip(sigmoid(sum(a * b for a, b in zip(w, x, strict=False))))


def walk_forward(rows: list[dict[str, Any]], genome: dict[str, Any], min_train: int = 150) -> list[tuple[dict[str, Any], float]]:
    """Out-of-sample predictions: each test day is predicted by a fit on strictly earlier days."""
    cat, model = genome["category"], genome["model"]
    rs = [r for r in rows if r["category"] == cat]
    out = []
    days = sorted({r["day"] for r in rs})
    for d in days:
        test = [r for r in rs if r["day"] == d]
        w = None
        if model not in ("market", "spotvol", "weather"):
            start = (datetime.fromisoformat(d) - timedelta(days=genome.get("window_days", 999))).date().isoformat()
            train = [r for r in rs if start <= r["day"] < d]
            xs = [(design(model, r["snap_mid"], r["model_p"]), r["result"]) for r in train]
            xs = [(x, y) for x, y in xs if x is not None]
            if len(xs) < min_train:
                continue
            w = fit_logistic([x for x, _ in xs], [y for _, y in xs], genome.get("ridge", 1.0), stack_prior(model))
        for r in test:
            p = predict(model, w, r)
            if p is not None:
                out.append((r, p))
    return out


def evaluate(preds: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
    if not preds:
        return {"n": 0}
    db = [(r["event_ticker"], brier(p, r["result"]) - brier(r["snap_mid"], r["result"])) for r, p in preds]
    dl = [(r["event_ticker"], logloss(p, r["result"]) - logloss(r["snap_mid"], r["result"])) for r, p in preds]
    mb, blo, bhi, bp = cluster_ci(db)
    ml, llo, lhi, _ = cluster_ci(dl)
    trades = []
    for r, p in preds:
        a, b = r["snap_ask"], r["snap_bid"]
        if p - a > kalshi_fee(a):
            trades.append((r["event_ticker"], r["result"] - a - kalshi_fee(a)))
        elif (1 - p) - (1 - b) > kalshi_fee(1 - b):
            trades.append((r["event_ticker"], (1 - r["result"]) - (1 - b) - kalshi_fee(1 - b)))
    tm, tlo, thi, tp = cluster_ci(trades) if trades else (0.0, 0.0, 0.0, 1.0)
    return {"n": len(preds), "events": len({r["event_ticker"] for r, _ in preds}),
            "brier": round(sum(brier(p, r["result"]) for r, p in preds) / len(preds), 4),
            "market_brier": round(sum(brier(r["snap_mid"], r["result"]) for r, _ in preds) / len(preds), 4),
            "logloss": round(sum(logloss(p, r["result"]) for r, p in preds) / len(preds), 4),
            "market_logloss": round(sum(logloss(r["snap_mid"], r["result"]) for r, _ in preds) / len(preds), 4),
            "brier_minus_market": round(mb, 5), "brier_diff_ci95": [round(blo, 5), round(bhi, 5)], "p_brier": bp,
            "logloss_minus_market": round(ml, 5), "logloss_diff_ci95": [round(llo, 5), round(lhi, 5)],
            "paper_trades": len(trades), "pnl_per_contract": round(tm, 4), "pnl_ci95": [round(tlo, 4), round(thi, 4)],
            "p_pnl": tp}


SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_agents (agent_id TEXT PRIMARY KEY, genome_json TEXT NOT NULL, status TEXT NOT NULL,
    generation INTEGER NOT NULL, parent_id TEXT, hired_at TEXT NOT NULL, fired_at TEXT, fire_reason TEXT,
    stats_json TEXT, weights_json TEXT, trade_enabled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pm_leaderboard (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, report_json TEXT NOT NULL);
"""

SEED_MODELS = ("market_cal", "spotvol", "weather", "model_cal", "stack")


def _aid(g: dict[str, Any]) -> str:
    import hashlib
    return "pm-" + g["category"].split("/")[0][:8] + "-" + g["model"] + "-" + hashlib.sha1(
        json.dumps(g, sort_keys=True).encode()).hexdigest()[:6]


def _life_event(life_db: str | None, aid: str, ev: str, detail: str) -> None:
    if not life_db or not os.path.isfile(life_db):
        return
    c = sqlite3.connect(life_db, timeout=30)
    try:
        c.execute("INSERT INTO lifecycle_events (at, agent_id, event, detail) VALUES (?,?,?,?)",
                  (datetime.now(UTC).isoformat(), aid, ev, detail[:500]))
        c.commit()
    except sqlite3.Error:
        pass
    finally:
        c.close()


def train(db: str, life_db: str | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    rng = rng or random.Random(int(time.time()))
    c = sqlite3.connect(db, timeout=60)
    c.executescript(SCHEMA)
    rows = load_rows(c)
    cats = sorted({r["category"] for r in rows})
    now = datetime.now(UTC).isoformat()
    # seed generation 0 per category
    for cat in cats:
        has_model = {r["model_kind"] for r in rows if r["category"] == cat} - {None}
        for m in SEED_MODELS:
            if m in ("spotvol", "weather") and m not in has_model:
                continue
            if m in ("model_cal", "stack") and not has_model:
                continue
            g = {"category": cat, "model": m, "window_days": 999, "ridge": 1.0}
            c.execute("INSERT OR IGNORE INTO pm_agents (agent_id, genome_json, status, generation, hired_at)"
                      " VALUES (?,?,?,?,?)", (_aid(g), json.dumps(g), "shadow", 0, now))
    c.commit()
    agents = c.execute("SELECT agent_id, genome_json, generation FROM pm_agents WHERE status!='fired'").fetchall()
    results = []
    for cat in cats:
        g = {"category": cat, "model": "market"}
        results.append(("benchmark-market-" + cat, g, evaluate(walk_forward(rows, g)), 0))
    for aid, gj, gen in agents:
        g = json.loads(gj)
        results.append((aid, g, evaluate(walk_forward(rows, g)), gen))
    tested = [i for i, (_, g, e, _) in enumerate(results) if g["model"] != "market" and e.get("n", 0) > 0]
    q_brier = bh([results[i][2]["p_brier"] for i in tested]) if tested else []
    q_pnl = bh([results[i][2]["p_pnl"] for i in tested]) if tested else []
    board, fired, hired = [], [], []
    for k, i in enumerate(tested):
        aid, g, e, gen = results[i]
        better = q_brier[k] and e["brier_diff_ci95"][1] < 0
        worse = q_brier[k] and e["brier_diff_ci95"][0] > 0
        trade_ok = bool(better and q_pnl[k] and e["pnl_ci95"][0] > 0)
        verdict = "beats market (OOS, BH q<0.05)" if better else ("fire: worse than market (OOS, BH q<0.05)" if worse else "no significant difference")
        board.append({"agent": aid, **g, **e, "verdict": verdict, "paper_trade_enabled": trade_ok})
        w = None
        if g["model"] in ("market_cal", "model_cal", "stack"):
            start = (datetime.now(UTC) - timedelta(days=g.get("window_days", 999))).date().isoformat()
            xs = [(design(g["model"], r["snap_mid"], r["model_p"]), r["result"]) for r in rows
                  if r["category"] == g["category"] and r["day"] >= start]
            xs = [(x, y) for x, y in xs if x is not None]
            if len(xs) >= 50:
                w = fit_logistic([x for x, _ in xs], [y for _, y in xs], g.get("ridge", 1.0), stack_prior(g["model"]))
        if worse:
            c.execute("UPDATE pm_agents SET status='fired', fired_at=?, fire_reason=?, stats_json=? WHERE agent_id=?",
                      (now, verdict, json.dumps(e), aid))
            fired.append(aid)
            _life_event(life_db, aid, "fired", f"{g['category']}/{g['model']}: Brier {e['brier']} vs market {e['market_brier']}")
        else:
            c.execute("UPDATE pm_agents SET status=?, stats_json=?, weights_json=?, trade_enabled=? WHERE agent_id=?",
                      ("earning" if trade_ok else "shadow", json.dumps(e), json.dumps(w), int(trade_ok), aid))
    # replace every fired agent with a mutation of the best surviving agent in its category
    for aid in fired:
        g = json.loads(c.execute("SELECT genome_json FROM pm_agents WHERE agent_id=?", (aid,)).fetchone()[0])
        alive = [b for b in board if b["category"] == g["category"] and b["agent"] not in fired
                 and b["model"] in ("market_cal", "model_cal", "stack")]
        parent = min(alive, key=lambda b: b["brier_minus_market"]) if alive else {**g, "agent": aid, "model": "market_cal"}
        child = {"category": g["category"], "model": parent["model"],
                 "window_days": rng.choice([7, 14, 21, 30, 999]), "ridge": rng.choice([0.3, 1.0, 3.0, 10.0])}
        cid = _aid(child)
        gen = (c.execute("SELECT generation FROM pm_agents WHERE agent_id=?", (aid,)).fetchone() or [0])[0] + 1
        if c.execute("INSERT OR IGNORE INTO pm_agents (agent_id, genome_json, status, generation, parent_id, hired_at)"
                     " VALUES (?,?,?,?,?,?)", (cid, json.dumps(child), "shadow", gen, parent["agent"], now)).rowcount:
            hired.append(cid)
            _life_event(life_db, cid, "hired", f"replaces {aid}; mutated from {parent['agent']}: {json.dumps(child)}")
    report = {"schema": "tev.pm_leaderboard.v1", "at": now, "mode": "paper/forecast only",
              "train_rows": len(rows), "by_category": {cat: sum(r["category"] == cat for r in rows) for cat in cats},
              "date_range": [rows[0]["close_time"], rows[-1]["close_time"]] if rows else None,
              "benchmarks": [{"category": g["category"], **e} for aid, g, e, _ in results if g["model"] == "market"],
              "leaderboard": sorted(board, key=lambda b: (b["category"], b["brier_minus_market"])),
              "fired": fired, "hired": hired,
              "multiple_testing": f"Benjamini-Hochberg q=0.05 over {len(tested)} agent tests (Brier and P&L separately)"}
    c.execute("INSERT INTO pm_leaderboard (at, report_json) VALUES (?,?)", (now, json.dumps(report)))
    c.commit()
    c.close()
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("NOEMA_PM_TRAIN_DB", "data/pm_train.db"))
    ap.add_argument("--life-db", default="data/agent_lifecycle.db")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=6 * 3600)
    ap.add_argument("--backfill-days", type=int, default=0, help="top up the training set before each fit")
    a = ap.parse_args()
    while True:
        if a.backfill_days:
            from .pm_history import run as backfill
            backfill(a.db, a.backfill_days)
        rep = train(a.db, a.life_db)
        print(json.dumps({k: rep[k] for k in ("at", "train_rows", "by_category", "fired", "hired")}), flush=True)
        if not a.loop:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
