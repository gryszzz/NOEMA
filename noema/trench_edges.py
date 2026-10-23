"""TRENCH-EDGES: honest walk-forward tests of five candidate pump.fun edges. PAPER/RESEARCH ONLY.

Protocol (same for every idea):
  * Coins = pump.fun launches seen after Helius trade decoding came online (consistent data source).
  * Time-ordered split into 5 equal-count folds by launch time. Folds 1-4 = discovery, fold 5 = held out.
  * Anything learned (wallet lists, thresholds, exit rule, filter) uses discovery data only; ideas that need
    training are walked forward inside discovery (train folds < k, test fold k, k=2..4) and purged so no
    training outcome window overlaps the test fold.
  * The single best discovery variant per idea is then run once on fold 5.
  * Pass = discovery Bonferroni p < 0.05 (over all variants tried for that idea) AND held-out mean > 0 with
    one-sided p < 0.05 and n >= 30.
  * Fills use trench_hft curve math (1% fee per side, own-size impact, priority fee per tx) at decision + latency.
  * COVERAGE: launch_stream only records a coin's trades for TRADE_FOLLOW_S (180 s) after creation (longer only
    when a watched wallet bought - a selection effect), so every simulated trade must close by t0 + COVER_S.
    Trades whose exit window would run past coverage are skipped, never marked at a frozen price.
Run: python -m noema.trench_edges report --db data/noema.db
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from itertools import pairwise
from typing import Any

from . import trench_hft as hft

COVER_S = 170.0           # usable trade coverage after creation (stream follows 180 s; 10 s safety margin)
FOLDS = 5
GRAD_MC = 410.0           # bonding-curve completion market cap in SOL (85 real SOL + 30 virtual)
PUMP_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{30,44}pump")


# ---------------------------------------------------------------- data
RPC_COVER_S = 120.0       # coins only seen via Helius (no launch_stream row): t0 = first trade, tighter coverage


def load(db: str, since: str | None = None, include_rpc: bool = False) -> tuple[dict[str, dict[str, Any]], float]:
    coins, end = hft.load(db, None)
    if include_rpc:
        coins.update(_rpc_only(db, set(coins)))
    if since is None:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = c.execute("SELECT MIN(received_at) FROM launch_trades WHERE source='helius'").fetchone()
        c.close()
        since = row[0] if row and row[0] else None
    t_since = hft._ts(since) + 60 if since else 0.0
    out = {}
    for m, v in coins.items():
        if v["t0"] < t_since:
            continue
        v["T"] = [x[0] for x in v["tr"]]
        v["cov"] = v["t0"] + (RPC_COVER_S if v.get("rpc_only") else COVER_S)
        out[m] = v
    return apply_gap_guard(out), end


GAP_S = 10.0  # a >10 s silence across ALL coins is a capture outage (feed/RPC drop or DB lock), not a quiet market


def capture_gaps(coins: dict[str, dict[str, Any]], gap_s: float = GAP_S) -> list[tuple[float, float]]:
    ts = sorted(t for c in coins.values() for t in c["T"])
    return [(a, b) for a, b in pairwise(ts) if b - a > gap_s]


def apply_gap_guard(coins: dict[str, dict[str, Any]], gap_s: float = GAP_S) -> dict[str, dict[str, Any]]:
    """Prices inside a capture outage look frozen (the META bug). Cut each coin's usable coverage at the first
    outage after its create, and drop coins created during an outage (their early tape is missing)."""
    gaps = capture_gaps(coins, gap_s)
    starts = [a for a, _b in gaps]
    out = {}
    for m, c in coins.items():
        i = bisect_right(starts, c["t0"]) - 1
        if i >= 0 and gaps[i][1] > c["t0"]:
            continue  # created inside an outage
        if i + 1 < len(gaps):
            c["cov"] = min(c["cov"], gaps[i + 1][0])
        out[m] = c
    return out


def _rpc_only(db: str, known: set[str]) -> dict[str, dict[str, Any]]:
    """Mints whose trades Helius decoded but whose create PumpPortal never delivered. t0 = first recorded trade
    (the stream starts following at the on-chain create, so the first trade is at or just after creation)."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict[str, dict[str, Any]] = {}
    for mint, at, side, sol, trader, mc in c.execute(
            "SELECT mint, received_at, side, sol, trader, market_cap_sol FROM launch_trades WHERE mint LIKE '%pump' "
            "AND source='helius' ORDER BY received_at"):
        if mint in known or not mc or mc <= 0 or side not in ("buy", "sell"):
            continue
        t = hft._ts(at)
        v = out.setdefault(mint, {"t0": t, "creator": None, "tr": [], "smart": set(), "rpc_only": True})
        v["tr"].append((t, side == "buy", float(sol or 0), trader, float(mc)))
    c.close()
    return {m: v for m, v in out.items() if len(v["tr"]) >= 3}


def creator_history(db: str) -> dict[str, list[float]]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    h: dict[str, list[float]] = defaultdict(list)
    for at, cr in c.execute("SELECT received_at, creator FROM launch_stream WHERE creator IS NOT NULL ORDER BY received_at"):
        h[cr].append(hft._ts(at))
    c.close()
    return h


def fold_edges(coins: dict[str, dict[str, Any]], folds: int = FOLDS) -> list[float]:
    t = sorted(v["t0"] for v in coins.values())
    return [t[int(len(t) * k / folds)] for k in range(1, folds)]


def fold_of(t: float, edges: list[float]) -> int:
    return bisect_right(edges, t)  # 0..folds-1


# ---------------------------------------------------------------- execution sim
def mc_at(coin: dict[str, Any], t: float) -> float | None:
    i = bisect_right(coin["T"], t) - 1
    return coin["tr"][i][4] if i >= 0 else None


def sim(coin: dict[str, Any], t_sig: float, lat: float, usd: float, sol_usd: float, prio: float,
        rule: dict[str, Any]) -> float | None:
    """Generic exit engine on the pump.fun curve. rule keys:
    tmax (s), sl (fraction, None=off), ladder [(ret, fraction_of_initial)], trail (fraction from peak, None=off),
    trail_after_tp (bool: trail only arms after the first ladder fill).
    Returns net return on SOL in, or None if the exit window is not fully covered by recorded trades."""
    tr, T = coin["tr"], coin["T"]
    te = t_sig + lat
    if te + rule["tmax"] + lat > coin["cov"]:
        return None
    mc_e = mc_at(coin, te)
    if mc_e is None:
        return None
    sol = usd / sol_usd
    toks = hft.buy_tokens(mc_e, sol)
    hold, out, txs, peak, filled = toks, 0.0, 1, mc_e, 0
    ladder = rule.get("ladder") or []
    sl, trail, after = rule.get("sl"), rule.get("trail"), rule.get("trail_after_tp", True)
    i = bisect_right(T, te)
    while i < len(tr) and tr[i][0] <= te + rule["tmax"]:
        x = tr[i]
        i += 1
        r = x[4] / mc_e - 1
        peak = max(peak, x[4])
        fill = mc_at(coin, x[0] + lat) or x[4]
        if filled < len(ladder) and r >= ladder[filled][0]:
            q = min(hold, toks * ladder[filled][1])
            out += hft.sell_sol(fill, q); hold -= q; txs += 1; filled += 1
            if hold <= 1e-12:
                break
            continue
        armed = trail is not None and (filled > 0 or not after)
        if (sl is not None and r <= -sl) or (armed and x[4] <= peak * (1 - trail)):
            out += hft.sell_sol(fill, hold); hold = 0; txs += 1
            break
    if hold > 1e-12:
        out += hft.sell_sol(mc_at(coin, te + rule["tmax"] + lat) or mc_e, hold); txs += 1
    return (out - sol - prio * txs) / sol


def hft_rule(r: dict[str, float]) -> dict[str, Any]:
    """Express a trench_hft.EXIT_RULES entry in the generic engine (used to cross-check the two sims)."""
    return {"tmax": r["tmax"], "sl": r["sl"], "ladder": [(r["tp"], r["part"])], "trail": r["trail"],
            "trail_after_tp": True}


EXITS: dict[str, dict[str, Any]] = {}
for _tm in (10, 20, 30, 60, 90, 120):
    EXITS[f"time{_tm}"] = {"tmax": _tm}
for _tr in (0.1, 0.2, 0.3, 0.5):
    for _tm in (60, 120):
        EXITS[f"trail{int(_tr*100)}_t{_tm}"] = {"tmax": _tm, "trail": _tr, "trail_after_tp": False}
for _tp in (0.2, 0.5, 1.0):
    for _sl in (0.1, 0.2, 0.35):
        EXITS[f"tp{int(_tp*100)}_sl{int(_sl*100)}_t120"] = {"tmax": 120, "sl": _sl, "ladder": [(_tp, 1.0)]}
EXITS["ladder25_60_trail25_sl30_t120"] = {"tmax": 120, "sl": 0.3, "ladder": [(0.25, 1 / 3), (0.6, 1 / 3)],
                                          "trail": 0.25, "trail_after_tp": True}
EXITS["ladder15_40_trail15_sl15_t60"] = {"tmax": 60, "sl": 0.15, "ladder": [(0.15, 1 / 3), (0.4, 1 / 3)],
                                         "trail": 0.15, "trail_after_tp": True}
EXITS["fresh_g1_tp30_sl7.9_t120"] = {"tmax": 120, "sl": 0.079, "ladder": [(0.30, 1.0)]}


# ---------------------------------------------------------------- stats
def stats(xs: list[float]) -> dict[str, Any]:
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "ci95": None, "p": 1.0, "hit": None}
    lo, hi = hft.bootstrap_ci(xs) if n >= 2 else (float("nan"), float("nan"))
    srt = sorted(xs)
    return {"n": n, "mean": round(sum(xs) / n, 4), "median": round(srt[n // 2], 4),
            "mean_ex_top1": round((sum(xs) - srt[-1]) / (n - 1), 4) if n > 1 else None,
            "ci95": [round(lo, 4), round(hi, 4)],
            "p": round(hft.t_p(xs), 4), "hit": round(sum(1 for x in xs if x > 0) / n, 3)}


def welch_p_less(a: list[float], b: list[float]) -> float:
    """One-sided p that mean(a) < mean(b) (normal approx)."""
    if len(a) < 3 or len(b) < 3:
        return 1.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b)) or 1e-9
    return 0.5 * math.erfc(((mb - ma) / se) / math.sqrt(2))


def verdict(disc: dict[str, Any], m: int, held: dict[str, Any]) -> dict[str, Any]:
    p_bonf = min(1.0, disc["p"] * max(m, 1))
    ok_d = disc["n"] > 0 and (disc["mean"] or 0) > 0 and p_bonf < 0.05
    ok_h = held["n"] >= 30 and (held["mean"] or 0) > 0 and held["p"] < 0.05
    return {"variants": m, "discovery_p_bonf": round(p_bonf, 4), "discovery_pass": ok_d, "heldout_pass": ok_h,
            "PASS": ok_d and ok_h}


# ---------------------------------------------------------------- idea 2: exits over entries (and baselines)
def baseline_entries(coins: dict[str, dict[str, Any]], kind: str) -> list[tuple[float, str]]:
    """'all15': every coin with >=5 trades at 15 s. 'fresh': the PR 233 fresh-launch pre-filter at 15/45 s."""
    from .trench_fresh import FreshConfig, evaluate
    cfg = FreshConfig()
    out = []
    for m, c in coins.items():
        if kind == "all15":
            if hft.features(c, 15):
                out.append((c["t0"] + 15, m))
            continue
        for age in (15, 45):
            f = hft.features(c, age)
            d, _ = evaluate(f, age, cfg)
            if d == "enter":
                out.append((c["t0"] + age, m))
                break
            if d == "drop":
                break
    return sorted(out)


def run_exits(coins, edges, entries, cost) -> dict[str, Any]:
    res = {}
    for name, rule in EXITS.items():
        d, h = [], []
        for t, m in entries:
            v = sim(coins[m], t, rule=rule, **cost)
            if v is None:
                continue
            (h if fold_of(coins[m]["t0"], edges) == FOLDS - 1 else d).append(v)
        res[name] = (d, h)
    best = max(res, key=lambda k: (sum(res[k][0]) / len(res[k][0])) if res[k][0] else -9)
    table = {k: {"discovery": stats(v[0]), "heldout": stats(v[1])} for k, v in res.items()}
    ds, hs = stats(res[best][0]), stats(res[best][1])
    return {"selected": best, "discovery": ds, "heldout": hs, **verdict(ds, len(EXITS), hs), "all_exits": table}


# ---------------------------------------------------------------- idea 1: copy proven wallets
def wallet_buys(coins, max_age: float) -> list[tuple[float, str, str]]:
    """First buy per (wallet, coin) within max_age of launch, excluding the creator's own buys."""
    out = []
    for m, c in coins.items():
        seen = set()
        for t, is_buy, _sol, w, _mc in c["tr"]:
            if t - c["t0"] > max_age:
                break
            if is_buy and w and w not in seen and w != c["creator"]:
                seen.add(w)
                out.append((t, w, m))
    out.sort()
    return out


def follow_ret(coin, t_buy, lat, h, cost) -> float | None:
    return sim(coin, t_buy, lat=lat, usd=cost["usd"], sol_usd=cost["sol_usd"], prio=cost["prio"], rule={"tmax": h})


def copy_wallets(coins, edges, cost, lat_copy: float) -> dict[str, Any]:
    buys = wallet_buys(coins, COVER_S - 60)
    score_h = 30.0
    fr = []
    for t, w, m in buys:
        v = follow_ret(coins[m], t, lat_copy, score_h, cost)
        if v is not None:
            fr.append((t, w, m, v, fold_of(coins[m]["t0"], edges)))
    variants = [{"min_n": mn, "min_t": mt, "exit": ex} for mn in (5, 10, 20) for mt in (1.0, 2.0, 3.0)
                for ex in ("time30", "time60", "trail20_t60", "ladder15_40_trail15_sl15_t60")]

    def pick(train_until: float) -> dict[str, tuple[int, float]]:
        agg: dict[str, list[float]] = defaultdict(list)
        for t, w, m, v, _f in fr:
            if t + lat_copy + score_h + lat_copy < train_until:  # purge: outcome known before the test fold
                agg[w].append(v)
        out = {}
        for w, xs in agg.items():
            n = len(xs)
            if n < 5:
                continue
            mu = sum(xs) / n
            sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1)) or 1e-9
            out[w] = (n, mu / (sd / math.sqrt(n)))
        return out

    def test(fold: int, var: dict[str, Any], scores) -> list[float]:
        chosen = {w for w, (n, tt) in scores.items() if n >= var["min_n"] and tt >= var["min_t"]}
        done, out = set(), []
        for t, w, m in buys:
            if w in chosen and m not in done and fold_of(coins[m]["t0"], edges) == fold:
                done.add(m)
                v = sim(coins[m], t, lat=lat_copy, usd=cost["usd"], sol_usd=cost["sol_usd"], prio=cost["prio"],
                        rule=EXITS[var["exit"]])
                if v is not None:
                    out.append(v)
        return out

    disc = {i: [] for i in range(len(variants))}
    for k in range(1, FOLDS - 1):
        sc = pick(edges[k - 1])
        for i, var in enumerate(variants):
            disc[i] += test(k, var, sc)
    best = max(disc, key=lambda i: (sum(disc[i]) / len(disc[i])) if len(disc[i]) >= 10 else -9)
    sc_final = pick(edges[FOLDS - 2])
    held = test(FOLDS - 1, variants[best], sc_final)
    ds, hs = stats(disc[best]), stats(held)
    n_sel = sum(1 for n, tt in sc_final.values() if n >= variants[best]["min_n"] and tt >= variants[best]["min_t"])
    top = sorted(((stats(v)["mean"] or -9, i) for i, v in disc.items() if len(v) >= 10), reverse=True)[:5]
    return {"latency_s": lat_copy, "first_buys_scored": len(fr), "selected": variants[best],
            "wallets_selected_for_heldout": n_sel, "discovery": ds, "heldout": hs,
            **verdict(ds, len(variants), hs),
            "top_discovery_variants": [{"variant": variants[i], **stats(disc[i])} for _, i in top]}


# ---------------------------------------------------------------- idea 3: graduation / curve-progress momentum
def progress_entries(coins, thr_mc: float) -> list[tuple[float, str]]:
    out = []
    for m, c in coins.items():
        for t, _b, _s, _w, mc in c["tr"]:
            if t > c["cov"]:
                break
            if mc >= thr_mc:
                out.append((t, m))
                break
    return sorted(out)


def graduation(coins, edges, cost) -> dict[str, Any]:
    exits = ["time10", "time30", "time60", "trail10_t60", "trail20_t60", "tp20_sl10_t120", "tp50_sl20_t120",
             "ladder15_40_trail15_sl15_t60"]
    grads = sum(1 for c in coins.values() if any(x[4] >= GRAD_MC for x in c["tr"] if x[0] <= c["cov"]))
    res = {}
    for thr in (100.0, 150.0, 250.0, 350.0):
        ent = progress_entries(coins, thr)
        for ex in exits:
            d, h = [], []
            for t, m in ent:
                v = sim(coins[m], t, rule=EXITS[ex], **cost)
                if v is not None:
                    (h if fold_of(coins[m]["t0"], edges) == FOLDS - 1 else d).append(v)
            res[(thr, ex)] = (d, h)
    cand = {k: v for k, v in res.items() if len(v[0]) >= 10}
    best = max(cand, key=lambda k: sum(cand[k][0]) / len(cand[k][0])) if cand else None
    if best is None:
        return {"graduated_within_coverage": grads, "note": "too few signals"}
    ds, hs = stats(res[best][0]), stats(res[best][1])
    return {"graduated_within_coverage": grads, "selected": {"cross_mc_sol": best[0], "exit": best[1]},
            "discovery": ds, "heldout": hs, **verdict(ds, len(res), hs),
            "by_threshold_n": {str(int(k[0])): len(v[0]) + len(v[1]) for k, v in res.items() if k[1] == "time30"},
            "note": "post-migration (PumpSwap) prices and migration events are not stored; only on-curve runs "
                    "inside the 170 s coverage window are testable"}


# ---------------------------------------------------------------- idea 4: X mention velocity
def x_mentions(db: str, inbox: str) -> dict[str, list[tuple[float, str]]]:
    import glob
    from datetime import datetime
    ment: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for f in glob.glob(f"{inbox}/**/*.json", recursive=True):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:  # noqa: BLE001, S112 - skip unreadable inbox files
            continue
        for p in ((d.get("raw") or {}).get("data") or []) if isinstance(d, dict) else []:
            for m in set(PUMP_RE.findall(p.get("text", ""))):
                ment[m].append((datetime.fromisoformat(p["created_at"]).timestamp(),
                                str(p.get("author_id"))))
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for t, a, ca in c.execute("SELECT text, author_handle, created_at FROM x_posts WHERE created_at IS NOT NULL"):
        for m in set(PUMP_RE.findall(t or "")):
            ment[m].append((hft._ts(ca.replace("Z", "+00:00")), a))
    c.close()
    return ment


def x_velocity(db, inbox, coins, edges, cost) -> dict[str, Any]:
    ment = x_mentions(db, inbox)
    in_stream = [m for m in ment if m in coins]
    sig: dict[int, list[tuple[float, str]]] = {1: [], 2: []}
    for m in in_stream:
        posts = sorted(set(ment[m]))
        authors: set[str] = set()
        for t, a in posts:
            if a in authors:
                continue
            authors.add(a)
            if len(authors) in sig:
                sig[len(authors)].append((t, m))
    out = {"pump_mints_mentioned": len(ment), "mentioned_and_in_launch_stream": len(in_stream)}
    for k, ent in sig.items():
        xs = [v for t, m in ent if (v := sim(coins[m], t, rule=EXITS["time60"], **cost)) is not None]
        out[f"signals_{k}_authors"] = len(ent)
        out[f"priceable_{k}_authors"] = stats(xs)
    out["PASS"] = False
    out["note"] = ("untestable: almost no X mention lands inside the 170 s window where we record a coin's trades; "
                   "need price paths for mentioned mints (extend follow on mention) before this can be tested")
    return out


# ---------------------------------------------------------------- idea 5: avoid-side filters
def avoid_filters(coins, edges, cost, creators) -> dict[str, Any]:
    age = 15
    rule = EXITS["time60"]
    rows = []
    for c in coins.values():
        f = hft.features(c, age)
        if not f:
            continue
        v = sim(c, c["t0"] + age, rule=rule, **cost)
        if v is None:
            continue
        prior = [t for t in creators.get(c["creator"], []) if t < c["t0"] - 1]
        f = dict(f, creator_prior=len(prior), creator_recent_1h=sum(1 for t in prior if t > c["t0"] - 3600))
        rows.append((fold_of(c["t0"], edges), f, v))
    filters = {
        "dev_sold": lambda f: f["dev_sold"] > 0,
        "top3_gt_0.5": lambda f: f["top3_share"] > 0.5, "top3_gt_0.7": lambda f: f["top3_share"] > 0.7,
        "snipers_1s_gt_0.3": lambda f: f["first1s_share"] > 0.3, "snipers_1s_gt_0.5": lambda f: f["first1s_share"] > 0.5,
        "bundle_cluster_gt_0.3": lambda f: f["cluster_share"] > 0.3, "bundle_cluster_gt_0.5": lambda f: f["cluster_share"] > 0.5,
        "one_buyer_gt_0.3": lambda f: f["size_max_share"] > 0.3, "one_buyer_gt_0.5": lambda f: f["size_max_share"] > 0.5,
        "serial_creator_ge1": lambda f: f["creator_prior"] >= 1, "serial_creator_ge3": lambda f: f["creator_prior"] >= 3,
        "creator_burst_1h_ge2": lambda f: f["creator_recent_1h"] >= 2,
        "sell_pressure_ofi_lt0": lambda f: f["ofi"] < 0, "down_25_from_peak": lambda f: f["dd_peak"] <= -0.25,
        "few_buyers_lt5": lambda f: f["uniq_buyers"] < 5,
    }
    disc_all = [v for k, _f, v in rows if k < FOLDS - 1]
    held_all = [v for k, _f, v in rows if k == FOLDS - 1]
    table, best, best_p = {}, None, 1.0
    for name, fn in filters.items():
        dk = [v for k, f, v in rows if k < FOLDS - 1 and not fn(f)]
        dr = [v for k, f, v in rows if k < FOLDS - 1 and fn(f)]
        hk = [v for k, f, v in rows if k == FOLDS - 1 and not fn(f)]
        hr = [v for k, f, v in rows if k == FOLDS - 1 and fn(f)]
        p_d = welch_p_less(dr, dk)
        p_h = welch_p_less(hr, hk)
        table[name] = {"removed_share_disc": round(len(dr) / max(1, len(dr) + len(dk)), 3),
                       "disc_removed_mean": stats(dr)["mean"], "disc_kept": stats(dk), "disc_p_removed_worse": round(p_d, 4),
                       "disc_p_bonf": round(min(1.0, p_d * len(filters)), 4),
                       "held_removed_mean": stats(hr)["mean"], "held_kept": stats(hk), "held_p_removed_worse": round(p_h, 4)}
        if len(dr) >= 20 and p_d < best_p:
            best, best_p = name, p_d
    combo = [n for n, r in table.items() if r["disc_p_bonf"] < 0.05 and r["held_p_removed_worse"] < 0.05]
    fns = [filters[n] for n in combo]
    hk = [v for k, f, v in rows if k == FOLDS - 1 and not any(fn(f) for fn in fns)]
    dk = [v for k, f, v in rows if k < FOLDS - 1 and not any(fn(f) for fn in fns)]
    return {"baseline": "every coin with >=5 trades at 15 s, 60 s time-stop exit",
            "baseline_discovery": stats(disc_all), "baseline_heldout": stats(held_all),
            "filters_passing_both": combo, "filtered_discovery": stats(dk), "filtered_heldout": stats(hk),
            "avoid_side_PASS": bool(combo),
            "edge_PASS": bool(combo) and stats(hk)["n"] >= 30 and (stats(hk)["mean"] or 0) > 0 and stats(hk)["p"] < 0.05,
            "best_single_filter": best, "filters": table}


# ---------------------------------------------------------------- idea 6: creator track-record veto
def creator_outcomes(allcoins: dict[str, dict[str, Any]], horizon: float = 3600.0, thr: float = 0.5
                     ) -> dict[str, list[tuple[float, float, bool]]]:
    """creator -> [(t0, known_at, reached_thr)] for every recorded launch. Outcome = max bonding-curve progress
    over recorded trades within `horizon` of create; it only counts as known at t0 + horizon (no lookahead).
    Note: trades are recorded ~180 s per coin, so 'within 1 h' is in practice mostly 'within the first 3 min'."""
    out: dict[str, list[tuple[float, float, bool]]] = defaultdict(list)
    for c in allcoins.values():
        if not c.get("creator"):
            continue
        mcs = [x[4] for x in c["tr"] if x[0] <= c["t0"] + horizon]
        prog = (hft.v_from_mc(max(mcs)) - 30) / hft.CURVE_SOL if mcs else 0.0
        out[c["creator"]].append((c["t0"], c["t0"] + horizon, prog >= thr))
    return out


def creator_state(hist: list[tuple[float, float, bool]], t: float) -> tuple[int, int]:
    """(prior coins with a known outcome at time t, how many of them reached the threshold)."""
    known = [ok for t0, ka, ok in hist if ka <= t and t0 < t]
    return len(known), sum(known)


VETO_RULES = {
    "veto_all_dead": lambda k, w: k >= 1 and w == 0,
    "veto_all_dead_ge2": lambda k, w: k >= 2 and w == 0,
    "only_prior_runner": lambda k, w: w == 0,
}
VETO_EXITS = ("time30", "time60", "tp20_sl10_t120", "fresh_g1_tp30_sl7.9_t120")


def creator_veto(coins, edges, cost, allcoins) -> dict[str, Any]:
    hist = creator_outcomes(allcoins)
    # in-sample label check (same definition as the other analysis), restricted to our coins
    lab: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for c in coins.values():
        if not c.get("creator"):
            continue
        k, w = creator_state(hist[c["creator"]], c["t0"])
        own = [ok for t0, _ka, ok in hist[c["creator"]] if t0 == c["t0"]]
        key = "no_prior" if k == 0 else ("prior_runner" if w else "prior_all_dead")
        lab[key][0] += 1
        lab[key][1] += int(bool(own and own[0]))
    label_check = {k: {"n": n, "p_reach_half_curve": round(s / n, 4) if n else None} for k, (n, s) in lab.items()}
    bases = {"fresh": baseline_entries(coins, "fresh"), "all15": baseline_entries(coins, "all15")}
    res = {}
    for bname, entries in bases.items():
        for rname, rule_fn in VETO_RULES.items():
            for ename in VETO_EXITS:
                buck = {"dk": [], "dv": [], "hk": [], "hv": []}
                for t, m in entries:
                    c = coins[m]
                    if not c.get("creator"):
                        continue
                    v = sim(c, t, rule=EXITS[ename], **cost)
                    if v is None:
                        continue
                    k, w = creator_state(hist[c["creator"]], t)
                    side = "h" if fold_of(c["t0"], edges) == FOLDS - 1 else "d"
                    buck[side + ("v" if rule_fn(k, w) else "k")].append(v)
                res[f"{bname}|{rname}|{ename}"] = buck
    m = len(res)

    def score(b):  # the hypothesis is the veto: pick the variant whose vetoed trades are most clearly worse
        return welch_p_less(b["dv"], b["dk"]) if len(b["dk"]) >= 20 and len(b["dv"]) >= 20 else 1.0

    best = min(res, key=lambda k: score(res[k]))
    b = res[best]
    ds, hs = stats(b["dk"]), stats(b["hk"])
    lift_d, lift_h = welch_p_less(b["dv"], b["dk"]), welch_p_less(b["hv"], b["hk"])
    hk3 = sorted(b["hk"])[:-3] if len(b["hk"]) > 3 else []
    table = {k: {"disc_kept": stats(v["dk"]), "disc_vetoed_mean": stats(v["dv"])["mean"], "disc_vetoed_n": len(v["dv"]),
                 "held_kept_mean": stats(v["hk"])["mean"], "held_kept_n": len(v["hk"])} for k, v in res.items()}
    return {"label_check_reach_half_curve": label_check, "selected": best,
            "discovery": ds, "discovery_vetoed": stats(b["dv"]), "discovery_p_vetoed_worse": round(lift_d, 4),
            "discovery_lift_p_bonf": round(min(1.0, lift_d * m), 4),
            "heldout": hs, "heldout_vetoed": stats(b["hv"]), "heldout_p_vetoed_worse": round(lift_h, 4),
            "heldout_kept_ex_top3": stats(hk3),
            "veto_lift_PASS": min(1.0, lift_d * m) < 0.05 and lift_h < 0.05,
            **verdict(ds, m, hs), "variants_table": table}


# ---------------------------------------------------------------- data check: frozen prices / capture gaps
def frozen_check(coins, cost) -> dict[str, Any]:
    """META's marks froze (exits at exactly -cost with zero price moves). Our marks are the market cap printed by
    each recorded trade, so a 'frozen' mark can only come from (a) no trades in the window or (b) capture gaps.
    Report both, plus global capture gaps across all coins."""
    n = flat = notr = 0
    for c in coins.values():
        if not hft.features(c, 15):
            continue
        te = c["t0"] + 15 + cost["lat"]
        if te + 60 + cost["lat"] > c["cov"]:
            continue
        i, j = bisect_right(c["T"], te), bisect_right(c["T"], te + 60)
        n += 1
        if j <= i:
            notr += 1
        elif len({x[4] for x in c["tr"][max(0, i - 1):j]}) == 1:
            flat += 1
    ts = sorted(t for c in coins.values() for t in c["T"])
    gaps = sorted(((b - a), a) for a, b in pairwise(ts) if b - a > 20)
    from datetime import UTC, datetime
    return {"entries_checked_15s_to_75s": n, "no_trades_in_60s_window": notr, "flat_mark_with_trades": flat,
            "share_flat_or_no_trades": round((flat + notr) / max(1, n), 4),
            "global_capture_gaps_gt20s": len(gaps),
            "largest_gaps": [(round(g, 1), datetime.fromtimestamp(a, UTC).isoformat(timespec="seconds"))
                             for g, a in gaps[-5:]]}


# ---------------------------------------------------------------- report
def report(db: str, inbox: str, lat: float, usd: float, sol_usd: float, prio: float,
           include_rpc: bool = False) -> dict[str, Any]:
    coins, end = load(db, include_rpc=include_rpc)
    edges = fold_edges(coins)
    cost = {"lat": lat, "usd": usd, "sol_usd": sol_usd, "prio": prio}
    from datetime import UTC, datetime
    iso = lambda t: datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")
    out: dict[str, Any] = {
        "coins": len(coins), "trades": sum(len(c["tr"]) for c in coins.values()),
        "window_utc": [iso(min(c["t0"] for c in coins.values())), iso(end)],
        "heldout_from_utc": iso(edges[-1]), "cost": cost, "coverage_s": COVER_S, "include_rpc_only": include_rpc,
    }
    out["2_exits_all15"] = run_exits(coins, edges, baseline_entries(coins, "all15"), cost)
    out["2_exits_fresh_filter"] = run_exits(coins, edges, baseline_entries(coins, "fresh"), cost)
    out["1_copy_wallets"] = copy_wallets(coins, edges, cost, lat_copy=lat)
    out["1_copy_wallets_lat3"] = copy_wallets(coins, edges, cost, lat_copy=3.0)
    out["3_graduation"] = graduation(coins, edges, cost)
    out["4_x_velocity"] = x_velocity(db, inbox, coins, edges, cost)
    out["5_avoid"] = avoid_filters(coins, edges, cost, creator_history(db))
    out["6_creator_veto"] = creator_veto(coins, edges, cost, hft.load(db, None)[0])
    out["data_frozen_check"] = frozen_check(coins, cost)
    return out


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["report"])
    ap.add_argument("--db", default="data/noema.db")
    ap.add_argument("--inbox", default="/workspace/noema-run/x-inbox")
    ap.add_argument("--lat", type=float, default=1.5)
    ap.add_argument("--usd", type=float, default=5.0)
    ap.add_argument("--sol-usd", type=float, default=104.0)
    ap.add_argument("--prio", type=float, default=0.0003)
    ap.add_argument("--include-rpc", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    r = report(a.db, a.inbox, a.lat, a.usd, a.sol_usd, a.prio, a.include_rpc)
    s = json.dumps(r, indent=1, default=str)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(s)
    print(s)


if __name__ == "__main__":  # pragma: no cover
    main()
