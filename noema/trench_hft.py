"""TRENCH-HFT research: tick features on fresh pump.fun coins, purged walk-forward models, curve-math execution sim.

Pure stdlib (no numpy). Uses launch_stream/launch_trades (our own receive timestamps, so latency is real) and the
smart-wallet watchlist. PAPER ONLY. Run: python -m noema.trench_hft report --db data/noema.db
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from typing import Any

from . import tape_quality as tq

K = 30.0 * 1.073e9            # pump.fun virtual reserves constant (SOL * tokens)
CURVE_SOL = 85.0              # real SOL to complete the curve (approx)
AGES = (15, 45, 120)          # decision ages (s since create)
HORIZONS = (30, 120, 600)     # label horizons (s after our entry)
FEE = 0.01                    # pump.fun bonding-curve fee per side


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def v_from_mc(mc: float) -> float:
    return math.sqrt(max(mc, 1e-9) * K / 1e9)


def buy_tokens(mc: float, sol: float) -> float:
    v = v_from_mc(mc)
    return K / v - K / (v + sol * (1 - FEE))


def sell_sol(mc: float, tokens: float) -> float:
    v = v_from_mc(mc)
    t = K / v
    return (v - K / (t + tokens)) * (1 - FEE)


QUALITY = tq.Stats()  # the last load()'s exclusion counts by reason (tape_quality rules)


def load_tape(c: sqlite3.Connection, where: str = "WHERE mint LIKE '%pump'", keep: Callable[[str], bool] | None = None
              ) -> tuple[dict[str, list[tuple]], float]:
    """Raw launch_trades rows per mint as tape_quality Row tuples (time-ordered) and the tape's last timestamp."""
    raw: dict[str, list[tuple]] = defaultdict(list)
    end = 0.0
    for mint, at, side, sol, tok, trader, mc, slot, q, _src in c.execute(tq.select_sql(c, where)):
        t = _ts(at)
        end = max(end, t)
        if keep is None or keep(mint):
            raw[mint].append(tq.row(t, side, sol, tok, mc, slot, q, trader))
    return raw, end


def clean_tape(raw: dict[str, list[tuple]], create_mc: dict[str, float | None], stats: tq.Stats,
               quality: bool = True) -> dict[str, tuple[list[tuple], set[str]]]:
    """Apply the tape_quality rules per coin -> {mint: ([(t, is_buy, sol, trader, mc)], flags)}.
    quality=False reproduces the old filter (mc > 0 and buy/sell) for before/after comparisons."""
    out: dict[str, tuple[list[tuple], set[str]]] = {}
    for mint, rows in raw.items():
        if quality:
            kept, _why, flags = tq.clean_coin(rows, create_mc.get(mint), stats)
        else:
            kept, flags = [r for r in rows if r[tq.MC] and r[tq.MC] > 0 and r[tq.SIDE] in ("buy", "sell")], set()
        out[mint] = ([(r[tq.T], r[tq.SIDE] == "buy", float(r[tq.SOL] or 0), r[tq.TRADER], float(r[tq.MC]))
                      for r in kept], flags)
    return out


def load(db: str, smart_path: str | None = None, quality: bool = True) -> tuple[dict[str, dict[str, Any]], float]:
    """Coins with >= 3 clean trades. Rows/coins failing the tape_quality sanity rules (impossible mcaps, zero-SOL
    non-SOL-quote curves, mayhem/non-standard curves, isolated price spikes, instant graduations) are excluded and
    counted by reason in trench_hft.QUALITY. Coins that graduated within 60 s carry flags={'fast_grad'}."""
    global QUALITY
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    coins: dict[str, dict[str, Any]] = {}
    create_mc: dict[str, float | None] = {}
    for mint, at, creator, ini, cmc in c.execute(
            "SELECT mint, received_at, creator, initial_sol, market_cap_sol FROM launch_stream WHERE mint LIKE '%pump'"):
        coins[mint] = {"t0": _ts(at), "creator": creator, "tr": [], "flags": set()}
        create_mc[mint] = float(cmc) if cmc else tq.create_mc_from_initial_sol(ini) if ini else None
    raw, end = load_tape(c, keep=coins.__contains__)
    c.close()
    QUALITY = tq.Stats()
    for mint, (tr, flags) in clean_tape(raw, create_mc, QUALITY, quality).items():
        coins[mint]["tr"], coins[mint]["flags"] = tr, flags
    smart: set[str] = set()
    if smart_path and os.path.isfile(smart_path):
        try:
            from .smart_wallets import load_watch
            smart = set(load_watch(smart_path))
        except Exception:  # noqa: BLE001
            smart = set()
    for v in coins.values():
        v["smart"] = smart
    return {m: v for m, v in coins.items() if len(v["tr"]) >= 3}, end


def mc_at(tr: list, t: float) -> float | None:
    last = None
    for x in tr:
        if x[0] > t:
            break
        last = x[4]
    return last


def features(coin: dict[str, Any], age: float) -> dict[str, float] | None:
    t0, cut = coin["t0"], coin["t0"] + age
    w = [x for x in coin["tr"] if x[0] <= cut]
    if len(w) < 5:
        return None
    buys = [x for x in w if x[1]]
    sells = [x for x in w if not x[1]]
    bs, ss = sum(x[2] for x in buys), sum(x[2] for x in sells)
    ub = {x[3] for x in buys}
    third = age / 3
    def seg(lo: float, hi: float, b: bool) -> float:
        return sum(x[2] for x in w if x[1] == b and t0 + lo < x[0] <= t0 + hi)
    early_ub = {x[3] for x in buys if x[0] <= t0 + third}
    late_ub = {x[3] for x in buys if x[0] > t0 + 2 * third}
    per = defaultdict(float)
    for x in buys:
        per[x[3]] += x[2]
    top3 = sum(sorted(per.values(), reverse=True)[:3]) / bs if bs else 0
    first1 = sum(1 for x in buys if x[0] <= t0 + 1.0) / max(len(buys), 1)
    clustered = sum(1 for a, b in pairwise(buys) if b[0] - a[0] < 0.3) / max(len(buys) - 1, 1)
    cr = coin["creator"]
    dev_b = sum(x[2] for x in buys if x[3] == cr)
    dev_s = sum(x[2] for x in sells if x[3] == cr)
    mcs = [x[4] for x in w]
    mc_now, mc0 = mcs[-1], mcs[0]
    prog = (v_from_mc(mc_now) - 30) / CURVE_SOL
    mc10 = mc_at(w, cut - 10) or mc0
    prog10 = (v_from_mc(mc10) - 30) / CURVE_SOL
    sizes = sorted(x[2] for x in buys) or [0]
    smart = coin["smart"]
    f = {
        "n": len(w), "ofi": (bs - ss) / (bs + ss) if bs + ss else 0, "buy_sol": bs, "sell_sol": ss,
        "uniq_buyers": len(ub), "buyer_rate": len(ub) / age, "buyer_accel": len(late_ub) - len(early_ub),
        "top3_share": top3, "first1s_share": first1, "cluster_share": clustered,
        "dev_sold": 1.0 if dev_s > 0 else 0.0, "dev_sell_ratio": dev_s / dev_b if dev_b else (1.0 if dev_s else 0.0),
        "smart_n": float(len(ub & smart)), "progress": prog, "velocity": (prog - prog10) / 10,
        "ret_launch": mc_now / mc0 - 1, "ret_10s": mc_now / mc10 - 1, "dd_peak": mc_now / max(mcs) - 1,
        "size_med": sizes[len(sizes) // 2], "size_max_share": sizes[-1] / bs if bs else 0,
        "sell_accel": seg(2 * third, age, False) - seg(third, 2 * third, False),
        "sell_traders": float(len({x[3] for x in sells})),
    }
    return f


def simulate(coin: dict[str, Any], t_sig: float, lat: float, usd: float, sol_usd: float, prio_sol: float,
             rule: dict[str, float]) -> float | None:
    """Net return (fraction of USD in) for one paper trade. Entry/exit fill at the curve state `lat` s after the
    decision tick; our own size moves the curve (constant-product math); 1% fee each side; priority fee per tx."""
    tr = coin["tr"]
    te = t_sig + lat
    mc_e = mc_at(tr, te)
    if mc_e is None:
        return None
    sol = usd / sol_usd
    toks = buy_tokens(mc_e, sol)
    px_e = mc_e
    hold, out, txs, peak = toks, 0.0, 1, mc_e
    tp, sl, tmax, trail, part = rule["tp"], rule["sl"], rule["tmax"], rule["trail"], rule["part"]
    took = False
    for x in tr:
        if x[0] <= te:
            continue
        if x[0] > te + tmax:
            break
        r = x[4] / px_e - 1
        peak = max(peak, x[4])
        fill = mc_at(tr, x[0] + lat) or x[4]
        if not took and r >= tp:
            q = hold * part
            out += sell_sol(fill, q); hold -= q; txs += 1; took = True
            if hold <= 0:
                break
            continue
        if r <= -sl or (took and x[4] <= peak * (1 - trail)):
            out += sell_sol(fill, hold); hold = 0; txs += 1
            break
    if hold > 0:
        out += sell_sol(mc_at(tr, te + tmax + lat) or px_e, hold); txs += 1
    return (out - sol - prio_sol * txs) / sol


def label(coin: dict[str, Any], te: float, h: float) -> tuple[float, int] | None:
    tr = coin["tr"]
    e, x = mc_at(tr, te), mc_at(tr, te + h)
    if not e or not x:
        return None
    lo = min([y[4] for y in tr if te < y[0] <= te + 600] or [e])
    return x / e - 1, int(lo < 0.5 * e)


# ---------- models (stdlib) ----------
def _std(X: list[list[float]]) -> tuple[list[float], list[float]]:
    m = [sum(c) / len(c) for c in zip(*X)]
    s = [max(1e-9, math.sqrt(sum((v - mu) ** 2 for v in c) / len(c))) for c, mu in zip(zip(*X), m)]
    return m, s


def fit_logit(X: list[list[float]], y: list[int], l2: float = 1.0, it: int = 300, lr: float = 0.1):
    m, s = _std(X)
    Z = [[max(-5, min(5, (v - a) / b)) for v, a, b in zip(r, m, s)] for r in X]
    w = [0.0] * (len(X[0]) + 1)
    n = len(Z)
    for _ in range(it):
        g = [0.0] * len(w)
        for z, t in zip(Z, y):
            p = 1 / (1 + math.exp(-max(-30, min(30, w[0] + sum(a * b for a, b in zip(w[1:], z))))))
            d = p - t
            g[0] += d
            for j, v in enumerate(z):
                g[j + 1] += d * v
        w = [wi - lr * (gi / n + (l2 * wi / n if j else 0)) for j, (wi, gi) in enumerate(zip(w, g))]
    def pred(r: list[float]) -> float:
        z = [max(-5, min(5, (v - a) / b)) for v, a, b in zip(r, m, s)]
        return 1 / (1 + math.exp(-max(-30, min(30, w[0] + sum(a * b for a, b in zip(w[1:], z))))))
    return pred


def fit_gbm(X: list[list[float]], y: list[int], rounds: int = 60, lr: float = 0.1, min_leaf: int = 20):
    """Boosted decision stumps on logloss, quantile split candidates. Shallow on purpose (robust, small n)."""
    n, d = len(X), len(X[0])
    cands = []
    for j in range(d):
        col = sorted(r[j] for r in X)
        cands.append(sorted({col[int(n * q)] for q in (0.1, 0.25, 0.5, 0.75, 0.9)}))
    base = math.log((sum(y) + 1) / (n - sum(y) + 1))
    F = [base] * n
    stumps = []
    for _ in range(rounds):
        p = [1 / (1 + math.exp(-f)) for f in F]
        g = [t - pi for t, pi in zip(y, p)]
        h = [pi * (1 - pi) + 1e-6 for pi in p]
        best = None
        for j in range(d):
            for thr in cands[j]:
                gl = hl = gr = hr = 0.0; nl = 0
                for r, gi, hi in zip(X, g, h):
                    if r[j] <= thr:
                        gl += gi; hl += hi; nl += 1
                    else:
                        gr += gi; hr += hi
                if nl < min_leaf or n - nl < min_leaf:
                    continue
                gain = gl * gl / hl + gr * gr / hr
                if best is None or gain > best[0]:
                    best = (gain, j, thr, gl / hl, gr / hr)
        if not best:
            break
        _, j, thr, vl, vr = best
        stumps.append((j, thr, lr * vl, lr * vr))
        F = [f + (lr * vl if r[j] <= thr else lr * vr) for f, r in zip(F, X)]
    def pred(r: list[float]) -> float:
        f = base + sum(a if r[j] <= t else b for j, t, a, b in stumps)
        return 1 / (1 + math.exp(-f))
    return pred


EXIT_RULES = [{"tp": tp, "sl": sl, "tmax": tm, "trail": 0.2, "part": 0.5}
              for tp in (0.3, 0.6) for sl in (0.2, 0.35) for tm in (120, 600)]


def bootstrap_ci(xs: list[float], b: int = 2000, seed: int = 7) -> tuple[float, float]:
    if len(xs) < 2:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    ms = sorted(sum(rnd.choice(xs) for _ in xs) / len(xs) for _ in range(b))
    return ms[int(0.025 * b)], ms[int(0.975 * b)]


def t_p(xs: list[float]) -> float:
    """One-sided p-value that mean > 0 (normal approx)."""
    n = len(xs)
    if n < 3:
        return 1.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) or 1e-9
    return 0.5 * math.erfc((m / (sd / math.sqrt(n))) / math.sqrt(2))


def build(coins: dict[str, dict[str, Any]], end: float, lat: float) -> list[dict[str, Any]]:
    rows = []
    for mint, c in coins.items():
        for age in AGES:
            te = c["t0"] + age + lat
            if te + max(HORIZONS) + 5 > end:
                continue
            f = features(c, age)
            if not f:
                continue
            labs = {h: label(c, te, h) for h in HORIZONS}
            if any(v is None for v in labs.values()):
                continue
            rows.append({"mint": mint, "age": age, "t_sig": c["t0"] + age, "f": f,
                         "ret": {h: labs[h][0] for h in HORIZONS}, "rug": labs[600][1]})
    return rows


def walk_forward(rows: list[dict[str, Any]], coins: dict, folds: int, lat: float, usd: float, sol_usd: float,
                 prio: float) -> dict[str, Any]:
    keys = sorted(rows[0]["f"]) if rows else []
    results = []
    base_cost = {}
    for age in AGES:
        rs = sorted((r for r in rows if r["age"] == age), key=lambda r: r["t_sig"])
        if len(rs) < folds * 30:
            continue
        # baseline: enter every coin at this age (no model), each exit rule
        for i, rule in enumerate(EXIT_RULES):
            pn = [simulate(coins[r["mint"]], r["t_sig"], lat, usd, sol_usd, prio, rule) for r in rs]
            pn = [p for p in pn if p is not None]
            base_cost[(age, i)] = sum(pn) / len(pn)
        blk = len(rs) // folds
        for model in ("logit", "gbm"):
            for h in HORIZONS:
                oos: list[tuple[dict, float, float]] = []
                for k in range(1, folds):
                    test = rs[k * blk:(k + 1) * blk if k < folds - 1 else len(rs)]
                    start = test[0]["t_sig"]
                    train = [r for r in rs[:k * blk] if r["t_sig"] + lat + max(HORIZONS) < start]  # purge
                    if len(train) < 40:
                        continue
                    X = [[r["f"][q] for q in keys] for r in train]
                    fit = fit_logit if model == "logit" else fit_gbm
                    up = fit(X, [int(r["ret"][h] > 0.03) for r in train])
                    rug = fit(X, [r["rug"] for r in train])
                    tr_s = sorted(up([r["f"][q] for q in keys]) for r in train)
                    for r in test:
                        x = [r["f"][q] for q in keys]
                        oos.append((r, up(x), rug(x), tr_s))
                for top in (0.1, 0.2):
                    for i, rule in enumerate(EXIT_RULES):
                        pn = []
                        for r, p, pr, tr_s in oos:
                            thr = tr_s[int(len(tr_s) * (1 - top))]
                            if p >= thr and pr < 0.5:
                                v = simulate(coins[r["mint"]], r["t_sig"], lat, usd, sol_usd, prio, rule)
                                if v is not None:
                                    pn.append(v)
                        results.append({"age": age, "model": model, "h": h, "top": top, "rule": i, "n": len(pn),
                                        "pnl": pn, "baseline_mean": base_cost[(age, i)]})
    return {"results": results, "features": keys}


def report(db: str, smart: str | None, lat: float, usd: float, sol_usd: float, prio: float, folds: int = 5) -> dict:
    coins, end = load(db, smart)
    rows = build(coins, end, lat)
    wf = walk_forward(rows, coins, folds, lat, usd, sol_usd, prio)
    res = wf["results"]
    m = len(res)
    out = []
    for r in res:
        pn = r["pnl"]
        if not pn:
            continue
        lo, hi = bootstrap_ci(pn)
        p = t_p(pn)
        out.append({k: r[k] for k in ("age", "model", "h", "top", "rule", "n")} | {
            "hit": round(sum(1 for x in pn if x > 0) / len(pn), 3), "mean": round(sum(pn) / len(pn), 4),
            "ci95": [round(lo, 4), round(hi, 4)], "p": round(p, 4), "p_bonf": round(min(1.0, p * m), 4),
            "baseline_mean": round(r["baseline_mean"], 4), "exit": EXIT_RULES[r["rule"]]})
    out.sort(key=lambda r: r["mean"] * math.sqrt(r["n"]), reverse=True)
    t_first = min((c["t0"] for c in coins.values()), default=0)
    return {"coins": len(coins), "samples": len(rows), "hours": round((end - t_first) / 3600, 2),
            "variants_tested": m, "latency_s": lat, "usd": usd, "sol_usd": sol_usd, "prio_sol": prio,
            "features": wf["features"], "top": out[:10], "n_positive_bonf": sum(1 for r in out if r["p_bonf"] < 0.05 and r["mean"] > 0)}


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["report"])
    ap.add_argument("--db", default="data/noema.db")
    ap.add_argument("--smart", default="data/smart_wallets.db")
    ap.add_argument("--lat", type=float, default=1.5)
    ap.add_argument("--usd", type=float, default=5.0)
    ap.add_argument("--sol-usd", type=float, default=105.0)
    ap.add_argument("--prio", type=float, default=0.0003)
    a = ap.parse_args()
    print(json.dumps(report(a.db, a.smart, a.lat, a.usd, a.sol_usd, a.prio), indent=1, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
