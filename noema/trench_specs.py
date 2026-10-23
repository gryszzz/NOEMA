"""Held-out tests of three externally sourced trench specs (docs/research/trench-specs-2026-10-22.md).

Paper research only. No lookahead: every feature uses data strictly before the decision time; time-ordered folds;
the last fold is held out; Bonferroni over each spec's grid. Costs: pump.fun curve fee (default 1.25 %/side, the
fee since 17 Oct 2026) + priority fee per tx, own-size curve impact, latency 0.5/1.5/3 s.

  S1  originality + creator pre-filter on the unchanged FRESH desk (orcACR rules, merged with the creator
      track-record veto): veto if creator launched before, name+symbol seen before, or dev initial buy > 4 SOL.
      Plus the literal orcACR exit arm (sell on first creator sell, else 20 s).
  S2  manipulation veto: wash_max (flips / (|net tokens| + 1)) and atomic buy+sell signatures; 6-cell grid.
  S3  sniper-overhang clearance re-entry (+ control without the inventory condition). TP +40 %, SL -20 %, 10 min,
      exit at 95 % curve.

The tape only records ~170 s of trades per coin and ~8 h total, so most of this is underpowered; results carry a
`preliminary` flag and nothing here may wire a desk.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from . import tape_quality as tq
from . import trench_edges as te
from . import trench_hft as hft

LATS = (0.5, 1.5, 3.0)
BAR_N = 150  # pre-registered NOEMA bar: >=150 trades, CI low > 0, still > 0 without the top 3
FRESH_EXIT = te.EXITS["fresh_g1_tp30_sl7.9_t120"]


def mc_for_progress(prog: float) -> float:
    v = 30 + prog * hft.CURVE_SOL
    return v * v * 1e9 / hft.K


def bar(xs: list[float]) -> dict[str, Any]:
    st = te.stats(xs)
    ex3 = sorted(xs)[:-3] if len(xs) > 3 else []
    ok = st["n"] >= BAR_N and st["ci95"] is not None and st["ci95"][0] > 0 and ex3 and sum(ex3) / len(ex3) > 0
    return {"n": st["n"], "meets_noema_bar": bool(ok), "mean_ex_top3": round(sum(ex3) / len(ex3), 4) if ex3 else None}


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float:
    """One-sided p that rate1 > rate2."""
    if min(n1, n2) == 0:
        return 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
    return 0.5 * math.erfc(((k1 / n1 - k2 / n2) / se) / math.sqrt(2))


# ---------------------------------------------------------------- extra data
def launches(db: str) -> list[tuple[float, str, str, str, float]]:
    """Every recorded launch (all history, for 'seen before'): (t, mint, creator, name|symbol key, initial_sol)."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = []
    for mint, at, cr, name, sym, ini in c.execute(
            "SELECT mint, received_at, creator, name, symbol, initial_sol FROM launch_stream ORDER BY received_at"):
        key = f"{(name or '').strip().lower()}|{(sym or '').strip().lower()}"
        out.append((hft._ts(at), mint, cr or "", key, float(ini or 0)))
    c.close()
    return out


def ext_trades(db: str, mints: set[str]) -> dict[str, list[tuple[float, bool, float, str, float, str]]]:
    """(t, is_buy, tokens, trader, mc, signature, sol) per mint, time-ordered."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict[str, list] = defaultdict(list)
    for sig, mint, at, side, tok, trader, mc, sol in c.execute(
            "SELECT signature, mint, received_at, side, tokens, trader, market_cap_sol, sol FROM launch_trades "
            "WHERE mint LIKE '%pump' ORDER BY received_at"):
        if mint in mints and tq.row_reason(side, sol, tok, mc) is None:  # same row rules as hft.load
            out[mint].append((hft._ts(at), side == "buy", float(tok or 0), trader or "", float(mc),
                              (sig or "").split(":")[0], float(sol or 0)))
    c.close()
    return out


def originality(coins, launch_rows) -> dict[str, dict[str, Any]]:
    """Per coin, features known at create: creator_prior_n, name_sym_seen_before, dev_init_sol."""
    seen_creator: dict[str, int] = defaultdict(int)
    seen_name: set[str] = set()
    feats = {}
    for _t, mint, cr, key, ini in launch_rows:
        feats[mint] = {"creator_prior_n": seen_creator[cr] if cr else 0,
                       "name_sym_seen_before": key in seen_name and key != "|", "dev_init_sol": ini}
        if cr:
            seen_creator[cr] += 1
        seen_name.add(key)
    return {m: feats[m] for m in coins if m in feats}


# ---------------------------------------------------------------- S1
def _all_priors_dead(hist, c, t):
    known, runners = te.creator_state(hist.get(c["creator"], []), t)
    return known >= 1 and runners == 0


def s1_rules(orig, hist):
    return {
        "orcACR": lambda m, c, t: (orig[m]["creator_prior_n"] > 0 or orig[m]["name_sym_seen_before"]
                                   or orig[m]["dev_init_sol"] > 4.0),
        "creator_track_record": lambda m, c, t: _all_priors_dead(hist, c, t),
    }


def split_eval(coins, edges, entries, veto_fn, rule, cost) -> dict[str, list[float]]:
    b: dict[str, list[float]] = {"dk": [], "dv": [], "hk": [], "hv": []}
    for t, m in entries:
        c = coins[m]
        v = te.sim(c, t, rule=rule, **cost)
        if v is None:
            continue
        side = "h" if te.fold_of(c["t0"], edges) == te.FOLDS - 1 else "d"
        b[side + ("v" if veto_fn(m, c, t) else "k")].append(v)
    return b


def summarize_veto(b, m_tests) -> dict[str, Any]:
    pd, ph = te.welch_p_less(b["dv"], b["dk"]), te.welch_p_less(b["hv"], b["hk"])
    return {"disc_passed": te.stats(b["dk"]), "disc_vetoed": te.stats(b["dv"]),
            "disc_p_vetoed_worse": round(pd, 4), "disc_p_bonf": round(min(1.0, pd * m_tests), 4),
            "held_passed": te.stats(b["hk"]), "held_vetoed": te.stats(b["hv"]),
            "held_p_vetoed_worse": round(ph, 4), "held_passed_bar": bar(b["hk"]),
            "veto_PASS": min(1.0, pd * m_tests) < 0.05 and ph < 0.05,
            "edge_PASS": bar(b["hk"])["meets_noema_bar"]}


def orcacr_exit(coin, lat, usd, sol_usd, prio) -> float | None:
    """Enter at create + latency; sell everything on the first creator sell (seen + latency), else at 20 s."""
    te_ = coin["t0"] + lat
    t_exit = te_ + 20
    for x in coin["tr"]:
        if x[0] > te_ and x[0] < t_exit and not x[1] and x[3] == coin["creator"]:
            t_exit = x[0]
            break
    if t_exit + lat > coin["cov"]:
        return None
    mc_e = te.mc_at(coin, te_) or (coin["tr"][0][4] if coin["tr"] else None)
    mc_x = te.mc_at(coin, t_exit + lat)
    if not mc_e or not mc_x:
        return None
    sol = usd / sol_usd
    return (hft.sell_sol(mc_x, hft.buy_tokens(mc_e, sol)) - sol - 2 * prio) / sol


def spec1(coins, edges, cost0, orig, hist) -> dict[str, Any]:
    rules = s1_rules(orig, hist)
    # (i) completion / half-curve rate on every launch (veto applied at create)
    comp = {}
    for rname, fn in rules.items():
        cnt = {k: [0, 0, 0] for k in ("passed", "vetoed")}  # n, reached 50 %, reached 100 % (within coverage)
        for m, c in coins.items():
            if m not in orig or not c.get("creator"):
                continue
            mx = max((x[4] for x in c["tr"] if x[0] <= c["cov"]), default=0)
            prog = (hft.v_from_mc(mx) - 30) / hft.CURVE_SOL if mx else 0
            k = "vetoed" if fn(m, c, c["t0"]) else "passed"
            cnt[k][0] += 1
            cnt[k][1] += prog >= 0.5
            cnt[k][2] += prog >= 1.0
        (n1, h1, f1), (n2, h2, f2) = cnt["passed"], cnt["vetoed"]
        comp[rname] = {"passed": {"n": n1, "half_curve": round(h1 / max(1, n1), 4), "complete": round(f1 / max(1, n1), 4)},
                       "vetoed": {"n": n2, "half_curve": round(h2 / max(1, n2), 4), "complete": round(f2 / max(1, n2), 4)},
                       "p_passed_more_half": round(two_prop_z(h1, n1, h2, n2), 5),
                       "p_passed_more_complete": round(two_prop_z(f1, n1, f2, n2), 5),
                       "vetoed_share": round(n2 / max(1, n1 + n2), 3)}
    # (ii) FRESH desk PnL, vetoed vs passed, 2 rules x 3 latencies (Bonferroni m=6); all15 as a larger-n secondary
    fresh, all15 = te.baseline_entries(coins, "fresh"), te.baseline_entries(coins, "all15")
    pnl, sec = {}, {}
    for lat in LATS:
        cost = dict(cost0, lat=lat)
        for rname, fn in rules.items():
            pnl[f"{rname}|lat{lat}"] = summarize_veto(split_eval(coins, edges, fresh, fn, FRESH_EXIT, cost), 6)
            sec[f"{rname}|lat{lat}"] = summarize_veto(
                split_eval(coins, edges, all15, fn, te.EXITS["time60"], cost), 6)
    # orcACR exit arm (literal rule at NOEMA latency, on veto-passed coins) -- expected to fail
    arm = {}
    for lat in LATS:
        d, h = [], []
        for m, c in coins.items():
            if m not in orig or not c.get("creator") or rules["orcACR"](m, c, c["t0"]):
                continue
            v = orcacr_exit(c, lat, cost0["usd"], cost0["sol_usd"], cost0["prio"])
            if v is not None:
                (h if te.fold_of(c["t0"], edges) == te.FOLDS - 1 else d).append(v)
        arm[f"lat{lat}"] = {"discovery": te.stats(d), "heldout": te.stats(h), "held_bar": bar(h),
                            **te.verdict(te.stats(d), 3, te.stats(h))}
    n_fresh = len(fresh)
    return {"completion_by_rule": comp, "fresh_desk_pnl": pnl, "all15_time60_secondary": sec, "orcacr_exit_arm": arm,
            "fresh_entries_total": n_fresh, "preliminary": True,
            "PASS": any(v["veto_PASS"] and v["edge_PASS"] for v in pnl.values())}


# ---------------------------------------------------------------- S2
def manip_features(ext: list, t: float) -> dict[str, float]:
    flips: dict[str, int] = defaultdict(int)
    last: dict[str, bool] = {}
    net: dict[str, float] = defaultdict(float)
    sigs: dict[str, set] = defaultdict(set)
    for x in ext:
        if x[0] > t:
            break
        tr = x[3]
        if tr in last and last[tr] != x[1]:
            flips[tr] += 1
        last[tr] = x[1]
        net[tr] += x[2] if x[1] else -x[2]
        if x[5]:
            sigs[x[5]].add(x[1])
    wash = max((f / (abs(net[w]) + 1) for w, f in flips.items()), default=0.0)
    return {"wash_max": wash, "atomic_rt_n": sum(1 for s in sigs.values() if len(s) == 2)}


def spec2(coins, edges, cost0, ext) -> dict[str, Any]:
    cost = dict(cost0, lat=1.5)
    grid = [(w, a) for w in (10, 25, 50) for a in (1, 3)]
    out = {}
    for base, entries, rule in (("fresh", te.baseline_entries(coins, "fresh"), FRESH_EXIT),
                                ("all15", te.baseline_entries(coins, "all15"), te.EXITS["time60"])):
        feats = {(t, m): manip_features(ext.get(m, []), t) for t, m in entries}
        cells = {}
        for w, a in grid:
            def fn(m, c, t, w=w, a=a, feats=feats):
                f = feats[(t, m)]
                return f["wash_max"] > w or f["atomic_rt_n"] >= a
            r = summarize_veto(split_eval(coins, edges, entries, fn, rule, cost), len(grid))
            # -50 % within the covered window (<= ~150 s; spec asks 5 min)
            dd = {"vetoed": [0, 0], "passed": [0, 0]}
            for t, m in entries:
                c = coins[m]
                mc_e = te.mc_at(c, t + cost["lat"])
                if not mc_e:
                    continue
                k = "vetoed" if fn(m, c, t) else "passed"
                dd[k][0] += 1
                dd[k][1] += any(x[4] <= mc_e * 0.5 for x in c["tr"] if t < x[0] <= min(c["cov"], t + 300))
            r["rate_minus50_within_cov"] = {k: round(v[1] / max(1, v[0]), 4) for k, v in dd.items()}
            cells[f"wash>{w}|atomic>={a}"] = r
        flagged = sum(1 for f in feats.values() if f["wash_max"] > 10 or f["atomic_rt_n"] >= 1)
        out[base] = {"entries": len(entries), "flagged_any_cell": flagged, "cells": cells,
                     "PASS": any(c["veto_PASS"] and c["edge_PASS"] for c in cells.values())}
    out["note"] = "fundlinked_snipers not computed: needs a Helius funder hop per creator (not in the tape)"
    out["preliminary"] = True
    out["PASS"] = out["fresh"]["PASS"]
    return out


# ---------------------------------------------------------------- S3
S3_RULE = {"tmax": 600, "sl": 0.20, "ladder": [(0.40, 1.0)], "exit_mc": mc_for_progress(0.95)}


def s3_triggers(c, ext, use_inventory: bool) -> float | None:
    t0 = c["t0"]
    cohort = {c["creator"]} if c.get("creator") else set()
    bought: dict[str, float] = defaultdict(float)
    held: dict[str, float] = defaultdict(float)
    for x in ext:
        if x[0] <= t0 + 2 and x[1]:
            cohort.add(x[3])
    win: list = []  # (t, trader, is_buy, sol)
    for x in ext:
        if x[3] in cohort:
            if x[1]:
                bought[x[3]] += x[2]; held[x[3]] += x[2]
            else:
                held[x[3]] = max(0.0, held[x[3]] - x[2])
        win.append((x[0], x[3], x[1], x[6]))
        if x[0] < t0 + 60:
            continue
        lo = x[0] - 60
        w = [y for y in win if y[0] > lo]
        ub = len({y[1] for y in w if y[2] and y[1] not in cohort})
        flow = sum(y[3] if y[2] else -y[3] for y in w)
        prog = (hft.v_from_mc(x[4]) - 30) / hft.CURVE_SOL
        tb = sum(bought.values())
        e_rem = (sum(held.values()) / tb) if tb > 0 else 0.0
        if ub >= 10 and flow > 0 and 0.25 <= prog <= 0.70 and (not use_inventory or e_rem <= 0.20):
            return x[0]
    return None


def spec3(coins, edges, cost0, ext) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm, inv in (("treatment_inventory_cleared", True), ("control_no_inventory_condition", False)):
        sig = {}
        for m, c in coins.items():
            t = s3_triggers(c, ext.get(m, []), inv)
            if t is not None:
                sig[m] = t
        res = {"triggers": len(sig)}
        for lat in LATS:
            cost = dict(cost0, lat=lat)
            d, h, diag = [], [], []
            for m, t in sig.items():
                c = coins[m]
                v = te.sim(c, t, rule=S3_RULE, **cost)
                if v is not None:
                    (h if te.fold_of(c["t0"], edges) == te.FOLDS - 1 else d).append(v)
                left = c["cov"] - (t + 2 * lat)
                if left >= 10:  # DIAGNOSTIC ONLY: same exits but forced out at end of coverage (not the spec)
                    dv = te.sim(c, t, rule=dict(S3_RULE, tmax=left), **cost)
                    if dv is not None:
                        diag.append(dv)
            res[f"lat{lat}"] = {"spec_discovery": te.stats(d), "spec_heldout": te.stats(h),
                                "diagnostic_truncated_at_coverage": te.stats(diag)}
        out[arm] = res
    out["note"] = ("the 10-minute time stop cannot be priced: trades are recorded only ~170 s after create, so spec "
                   "trades are counted only when the whole 10 min is covered (WATCH-extended coins). The "
                   "truncated-at-coverage numbers are a diagnostic, not the pre-registered rule.")
    out["preliminary"] = True
    out["PASS"] = False
    return out


# ---------------------------------------------------------------- report
def report(db: str, fee: float, usd: float, sol_usd: float, prio: float) -> dict[str, Any]:
    hft.FEE = fee
    coins, end = te.load(db)
    edges = te.fold_edges(coins)
    cost0 = {"lat": 1.5, "usd": usd, "sol_usd": sol_usd, "prio": prio}
    allc = hft.load(db, None)[0]
    hist = te.creator_outcomes(allc)
    orig = originality(coins, launches(db))
    ext = ext_trades(db, set(coins))
    span_h = (end - min(c["t0"] for c in coins.values())) / 3600
    return {"coins": len(coins), "tape_hours": round(span_h, 2), "fee_per_side": fee,
            "heldout_from_utc": datetime.fromtimestamp(edges[-1], UTC).isoformat(timespec="seconds"),
            "S1_originality_creator": spec1(coins, edges, cost0, orig, hist),
            "S2_manipulation_veto": spec2(coins, edges, cost0, ext),
            "S3_sniper_overhang_reentry": spec3(coins, edges, cost0, ext)}


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/noema.db")
    ap.add_argument("--fee", type=float, default=0.0125)
    ap.add_argument("--usd", type=float, default=5.0)
    ap.add_argument("--sol-usd", type=float, default=104.0)
    ap.add_argument("--prio", type=float, default=0.0003)
    ap.add_argument("--out")
    a = ap.parse_args()
    s = json.dumps(report(a.db, a.fee, a.usd, a.sol_usd, a.prio), indent=1, default=str)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(s)
    print(s)


if __name__ == "__main__":  # pragma: no cover
    main()
