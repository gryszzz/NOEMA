"""EDGE MINING: honest walk-forward tests of simple rules on data NOEMA already stores.

Read-only against the main DB. Every rule is chosen on the EARLIER part of the data (train) and
then scored once on the LATER part (holdout) after costs. Holdout p-values are corrected for the
number of rules carried into the holdout (Benjamini-Hochberg). A rule "survives" only if
holdout n >= MIN_N, the bootstrap 95% lower bound of its mean net return is > 0, and its
BH-adjusted p < 0.05. Survivors feed shadow/paper agents only; nothing here places orders.
"""
from __future__ import annotations

import json
import math
import os
import random
import sqlite3
from datetime import UTC, datetime
from typing import Any

MIN_N = int(os.getenv("NOEMA_EDGE_MIN_N", "30"))
HORIZON = 3600                    # seconds after capture; the 1h counterfactual mark
RETURN_CAP = 3.0                  # cap +300%: thin-pool prints above this are not exitable
FEE_ROUND_TRIP = 0.02             # DEX swap fees + priority, both legs
NOTIONAL_USD = 25.0               # paper clip size used for constant-product slippage
TRAIN_FRACTION = 0.6
QUANTILES = (0.2, 0.4, 0.6, 0.8)
BOOT = int(os.getenv("NOEMA_EDGE_BOOTSTRAP", "2000"))


def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _has(c: sqlite3.Connection, table: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def net_return(final_return: float, liquidity_usd: float) -> float:
    """Net per-trade return after fees and two-leg constant-product slippage, upside capped."""
    gross = max(-1.0, min(RETURN_CAP, float(final_return)))
    slip = 2.0 * NOTIONAL_USD / max(float(liquidity_usd or 0.0), 1.0)
    return gross - FEE_ROUND_TRIP - min(slip, 1.0)


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def bootstrap(xs: list[float], seed: int = 7, n: int | None = None) -> tuple[float, float, float]:
    """(lo95, hi95, one-sided p for mean <= 0)."""
    if len(xs) < 2:
        return (float("nan"), float("nan"), 1.0)
    n = n or BOOT
    rng = random.Random(seed)
    k = len(xs)
    ms = sorted(sum(xs[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    p = (sum(1 for m in ms if m <= 0) + 1) / (n + 1)
    return (ms[int(0.025 * n)], ms[int(0.975 * n) - 1], p)


def bh_adjust(ps: list[float]) -> list[float]:
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    adj = [1.0] * m
    best = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        best = min(best, ps[i] * m / rank)
        adj[i] = best
    return adj


# ---------------------------------------------------------------- trench pool sweeps
FEATURES = ("age_seconds", "buyer_acceleration", "buyer_growth_fraction", "creator_supply_fraction",
            "holder_hhi", "liquidity_growth_fraction", "max_drawdown_fraction", "observations",
            "organic_buyer_share", "organic_score", "organic_volume_fraction", "participation_balance",
            "return_fraction", "signed_flow_imbalance", "top5_holder_fraction", "top_holder_fraction",
            "wash_trade_probability")


def load_trench(db: str, horizon: int = HORIZON) -> list[dict[str, Any]]:
    c = _ro(db)
    try:
        if not (_has(c, "trench_candidates") and _has(c, "trench_counterfactuals")):
            return []
        rows = c.execute(
            "SELECT t.candidate_id, t.token_mint, t.captured_at, t.reference_liquidity_usd liq, t.disposition,"
            " t.survival_risk, t.opportunity_score, t.assessment_json, f.final_return_fraction ret"
            " FROM trench_candidates t JOIN trench_counterfactuals f ON f.candidate_id=t.candidate_id"
            " WHERE f.horizon_seconds=? ORDER BY t.captured_at", (horizon,)).fetchall()
    finally:
        c.close()
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["token_mint"] in seen:          # one sample per token: repeat sightings are not independent
            continue
        seen.add(r["token_mint"])
        try:
            feats = json.loads(r["assessment_json"]).get("features", {}) or {}
        except (TypeError, ValueError):
            feats = {}
        x = {k: feats.get(k) for k in FEATURES}
        x.update(liquidity_usd=r["liq"], survival_risk=r["survival_risk"],
                 opportunity_score=r["opportunity_score"], observe=1.0 if r["disposition"] == "observe" else 0.0)
        out.append({"id": r["candidate_id"], "at": r["captured_at"], "x": x, "liq": r["liq"],
                    "disposition": r["disposition"], "gross": float(r["ret"]),
                    "net": net_return(r["ret"], r["liq"])})
    return out


def rule_match(x: dict[str, Any], filters: list[dict[str, Any]]) -> bool:
    for f in filters:
        v = x.get(f["f"])
        if v is None:
            return False
        if f["op"] == ">=" and not v >= f["v"]:
            return False
        if f["op"] == "<=" and not v <= f["v"]:
            return False
    return True


def rule_name(filters: list[dict[str, Any]]) -> str:
    return " & ".join(f"{f['f']} {f['op']} {f['v']:.4g}" for f in filters) or "all"


def _quantile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def candidate_rules(train: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rules: list[list[dict[str, Any]]] = [[]]
    keys = list(train[0]["x"].keys()) if train else []
    for k in keys:
        vals = [s["x"][k] for s in train if s["x"].get(k) is not None]
        if len(vals) < MIN_N:
            continue
        for q in QUANTILES:
            t = _quantile(vals, q)
            rules.append([{"f": k, "op": ">=", "v": t}])
            rules.append([{"f": k, "op": "<=", "v": t}])
    # liquidity floors are the obvious cost control; pair them with each single-feature rule
    liqs = [s["liq"] for s in train]
    if liqs:
        floor = _quantile(liqs, 0.6)
        rules += [[{"f": "liquidity_usd", "op": ">=", "v": floor}] + r for r in rules[1:]
                  if r[0]["f"] != "liquidity_usd"]
    uniq: dict[str, list[dict[str, Any]]] = {}
    for r in rules:
        uniq.setdefault(rule_name(r), r)
    return list(uniq.values())


def mine_trench(db: str) -> dict[str, Any]:
    data = load_trench(db)
    res: dict[str, Any] = {"family": "trench_pool_sweeps", "horizon_s": HORIZON, "samples": len(data),
                           "costs": {"fee_round_trip": FEE_ROUND_TRIP, "notional_usd": NOTIONAL_USD,
                                     "return_cap": RETURN_CAP}}
    if len(data) < 2 * MIN_N:
        res.update(status="insufficient_data", candidates=[], survivors=[], rules_tested=0)
        return res
    cut = int(len(data) * TRAIN_FRACTION)
    train, test = data[:cut], data[cut:]
    res.update(train_window=[train[0]["at"], train[-1]["at"]], test_window=[test[0]["at"], test[-1]["at"]],
               baseline_all={"n": len(data), "mean_net": round(mean([s["net"] for s in data]), 4),
                             "median_gross": round(sorted(s["gross"] for s in data)[len(data) // 2], 4)})
    rules = candidate_rules(train)
    carried = []
    for r in rules:
        tr = [s["net"] for s in train if rule_match(s["x"], r)]
        if len(tr) >= MIN_N and mean(tr) > 0:
            carried.append((r, tr))
    best_train = sorted(((mean(v), len(v), rule_name(r)) for r in rules
                         for v in [[s["net"] for s in train if rule_match(s["x"], r)]] if len(v) >= MIN_N),
                        reverse=True)[:5]
    res["best_train_rules"] = [{"rule": n, "train_n": k, "train_mean_net": round(m, 4)} for m, k, n in best_train]
    out = []
    for i, (r, tr) in enumerate(carried):
        te = [s["net"] for s in test if rule_match(s["x"], r)]
        lo, hi, p = bootstrap(te, seed=i) if len(te) >= 2 else (float("nan"), float("nan"), 1.0)
        out.append({"rule": rule_name(r), "filters": r, "train_n": len(tr), "train_mean": round(mean(tr), 4),
                    "test_n": len(te), "test_mean": round(mean(te), 4) if te else None,
                    "ci95": [round(lo, 4), round(hi, 4)] if te and not math.isnan(lo) else None,
                    "p": p, "win_rate": round(sum(1 for v in te if v > 0) / len(te), 3) if te else None})
    adj = bh_adjust([o["p"] for o in out]) if out else []
    for o, a in zip(out, adj):
        o["p_bh"] = round(a, 4)
        o["p"] = round(o["p"], 4)
        o["survives"] = bool(o["test_n"] >= MIN_N and o["ci95"] and o["ci95"][0] > 0 and a < 0.05)
    out.sort(key=lambda o: (not o["survives"], -(o["test_mean"] if o["test_mean"] is not None else -9)))
    res.update(status="ok", rules_tested=len(rules), rules_carried_to_holdout=len(carried),
               candidates=out[:15], survivors=[o for o in out if o["survives"]])
    return res


# ---------------------------------------------------------------- prediction-market price bands
def kalshi_fee(p: float) -> float:
    return math.ceil(0.07 * p * (1 - p) * 100) / 100


def mine_prediction_bands(db: str) -> dict[str, Any]:
    """Favourite/longshot by entry-price band on resolved markets we snapshotted before resolution."""
    c = _ro(db)
    try:
        if not (_has(c, "market_snapshots") and _has(c, "outcomes")):
            return {"family": "prediction_price_bands", "status": "insufficient_data", "samples": 0}
        rows = c.execute(
            "SELECT s.venue, s.market_id, MIN(s.captured_at) at, s.snapshot_json, o.outcome_yes"
            " FROM market_snapshots s JOIN outcomes o ON o.venue=s.venue AND o.market_id=s.market_id"
            " WHERE s.valid=1 AND (o.resolved_at IS NULL OR s.captured_at < o.resolved_at)"
            " GROUP BY s.venue, s.market_id ORDER BY at").fetchall()
    finally:
        c.close()
    samples = []
    for r in rows:
        try:
            snap = json.loads(r["snapshot_json"])
            ya = float(snap["yes_ask"])
        except (TypeError, ValueError, KeyError):
            continue
        if 0 < ya < 1:
            samples.append({"at": r["at"], "ask": ya, "net": (1.0 if r["outcome_yes"] else 0.0) - ya - kalshi_fee(ya)})
    res: dict[str, Any] = {"family": "prediction_price_bands", "samples": len(samples)}
    if len(samples) < 2 * MIN_N:
        res.update(status="insufficient_data",
                   note="markets we snapshotted have almost never resolved yet; need weeks of snapshots")
        return res
    cut = int(len(samples) * TRAIN_FRACTION)
    test = samples[cut:]
    bands = [(0.0, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 0.9), (0.9, 1.0)]
    out = []
    for i, (a, b) in enumerate(bands):
        te = [s["net"] for s in test if a <= s["ask"] < b]
        lo, hi, p = bootstrap(te, seed=i)
        out.append({"rule": f"buy YES ask in [{a},{b})", "test_n": len(te), "test_mean": round(mean(te), 4),
                    "ci95": [round(lo, 4), round(hi, 4)] if len(te) > 1 else None, "p": p})
    for o, adj in zip(out, bh_adjust([o["p"] for o in out])):
        o["p_bh"] = round(adj, 4)
        o["survives"] = bool(o["test_n"] >= MIN_N and o["ci95"] and o["ci95"][0] > 0 and adj < 0.05)
    res.update(status="ok", candidates=out, survivors=[o for o in out if o["survives"]])
    return res


# ---------------------------------------------------------------- descriptive families
def describe_other(db: str, feed_db: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    c = _ro(db)
    try:
        if _has(c, "xv_pairs"):
            n = c.execute("SELECT COUNT(*) FROM xv_pairs").fetchone()[0]
            pos = 0
            best = None
            for (bj,) in c.execute("SELECT best_json FROM xv_pairs"):
                try:
                    b = json.loads(bj or "{}") or {}
                except ValueError:
                    continue
                v = b.get("edge")
                if isinstance(v, (int, float)):
                    best = v if best is None else max(best, v)
                    pos += v > 0
            out.append({"family": "cross_venue_gaps", "status": "no_outcomes_needed_but_no_size",
                        "pairs": n, "positive_after_fees": pos, "best_net_edge": best,
                        "note": "locked-in arb needs no prediction; edges seen are sub-cent, capacity unknown"})
        if _has(c, "accuracy_resolutions"):
            n = c.execute("SELECT COUNT(*) FROM accuracy_resolutions").fetchone()[0]
            out.append({"family": "forecast_vs_market", "status": "see /api/roster",
                        "resolved": n, "note": "agent Brier vs market price is scored per agent in the roster"})
        for t, label in (("x_market_snapshots", "x_timing_vs_repricing"),
                         ("news_market_links", "news_timing_vs_repricing")):
            if _has(c, t):
                n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                out.append({"family": label, "status": "insufficient_data", "linked_rows": n,
                            "note": "need repeated quotes of the linked market before AND after each post/article"})
    finally:
        c.close()
    if feed_db and os.path.isfile(feed_db):
        f = _ro(feed_db)
        try:
            if _has(f, "opp_items"):
                rows = f.execute("SELECT source, ref_price, last_price FROM opp_items WHERE ref_price>0"
                                 " AND last_price IS NOT NULL").fetchall()
                by: dict[str, list[float]] = {}
                for r in rows:
                    by.setdefault(r["source"], []).append(r["last_price"] / r["ref_price"] - 1)
                out.append({"family": "opportunity_history", "status": "insufficient_data" if len(rows) < MIN_N else "descriptive",
                            "items": len(rows), "by_source": {k: {"n": len(v), "mean_move": round(mean(v), 4)}
                                                              for k, v in by.items()}})
        finally:
            f.close()
    return out


def mine_all(db: str, feed_db: str | None = None) -> dict[str, Any]:
    trench = mine_trench(db)
    pm = mine_prediction_bands(db)
    survivors = [dict(s, family="trench_pool_sweeps") for s in trench.get("survivors", [])] + \
                [dict(s, family="prediction_price_bands") for s in pm.get("survivors", [])]
    return {"schema": "tev.edge_mining.v1", "at": datetime.now(UTC).isoformat(),
            "method": "walk-forward: rules picked on first 60% by time, scored once on last 40%; "
                      "bootstrap 95% CI; Benjamini-Hochberg across rules carried to holdout",
            "families": [trench, pm] + describe_other(db, feed_db),
            "survivors": survivors,
            "verdict": (f"{len(survivors)} rule(s) survived fees + multiple-testing correction (paper/shadow only)"
                        if survivors else "nothing survived fees + multiple-testing correction")}
