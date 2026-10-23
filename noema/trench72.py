"""72 h on-chain rerun of the trench edge study (pre-registered in
docs/research/trench-72h-prereg-2026-10-23.md). Research / paper only: reads parquet tapes, never trades,
never touches wallets or keys.

Run (needs numpy, numba, scipy, pyarrow, duckdb -- research venv, not NOEMA runtime deps):
    python -m noema.trench72 --work /workspace/trench72_work --out docs/research/trench-72h-2026-10-23.json
`--work` holds the compact tapes built from /workspace/marketdata/noema/pumpfun (epochs 1062-1064 only)."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

SPLIT_SLOT = 459_648_000          # held-out = epoch 1064
E1063 = 459_216_000
E1062_SCORE_END = 459_000_000
START_SLOT = 458_784_000
END_SLOT = 460_079_999
EPOCH_LEN = 432_000
N_FAMILY = 178
SOL_MINT = "So11111111111111111111111111111111111111112"
MD = "/workspace/marketdata/noema"


def _exits() -> dict[str, dict[str, Any]]:
    from .trench_edges import EXITS
    ex = dict(EXITS)
    ex["time300"] = {"tmax": 300}
    ex["time600"] = {"tmax": 600}
    ex["trail20_t300"] = {"tmax": 300, "trail": 0.2, "trail_after_tp": False}
    ex["tp50_sl20_t600"] = {"tmax": 600, "sl": 0.2, "ladder": [(0.5, 1.0)]}
    for t in (1800, 21600):
        ex[f"time{t}"] = {"tmax": t}
    ex["trail20_t21600"] = {"tmax": 21600, "trail": 0.2, "trail_after_tp": False}
    ex["tp50_sl20_t21600"] = {"tmax": 21600, "sl": 0.2, "ladder": [(0.5, 1.0)]}
    ex["reentry_tp40_sl20_t600_p95"] = {"tmax": 600, "sl": 0.2, "ladder": [(0.4, 1.0)], "exit_prog": 0.95}
    return ex


EXIT_LIB30 = None  # filled in main (26 trench_edges + 4 long)


def rule_params(r: dict[str, Any]) -> tuple:
    lad = list(r.get("ladder") or [])
    l1 = lad[0] if lad else (0.0, 0.0)
    l2 = lad[1] if len(lad) > 1 else (0.0, 0.0)
    sl = r.get("sl")
    tr = r.get("trail")
    ep = r.get("exit_prog")
    return (float(r["tmax"]), -1.0 if sl is None else float(sl), float(l1[0]), float(l1[1]), float(l2[0]),
            float(l2[1]), -1.0 if tr is None else float(tr), bool(r.get("trail_after_tp", True)),
            -1.0 if ep is None else float(ep))


# ---------------------------------------------------------------- stats
def tstat_p(x) -> float:
    import numpy as np
    from scipy import stats as st
    n = len(x)
    if n < 3:
        return 1.0
    sd = float(np.std(x, ddof=1))
    if sd == 0:
        return 0.0 if float(np.mean(x)) > 0 else 1.0
    return float(st.t.sf(float(np.mean(x)) / (sd / math.sqrt(n)), n - 1))


def welch_less(a, b) -> float:
    """One-sided p that mean(a) < mean(b)."""
    import numpy as np
    from scipy import stats as st
    if len(a) < 3 or len(b) < 3:
        return 1.0
    r = st.ttest_ind(np.asarray(a), np.asarray(b), equal_var=False, alternative="less")
    return float(r.pvalue) if not math.isnan(r.pvalue) else 1.0


def describe(x, boot: bool = True) -> dict[str, Any]:
    import numpy as np
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": None}
    s = np.sort(x)
    d = {"n": n, "mean": round(float(x.mean()), 5), "median": round(float(np.median(x)), 5),
         "ex_top3": round(float(s[:-3].mean()), 5) if n > 3 else None,
         "hit": round(float((x > 0).mean()), 4), "p": round(tstat_p(x), 6)}
    if boot and n >= 2:
        rng = np.random.default_rng(7)
        bm = np.array([x[rng.integers(0, n, n)].mean() for _ in range(2000)])
        d["ci95"] = [round(float(np.quantile(bm, 0.025)), 5), round(float(np.quantile(bm, 0.975)), 5)]
    return d


def holm(ps: list[float]) -> list[float]:
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    out = [1.0] * m
    run = 0.0
    for k, i in enumerate(order):
        run = max(run, min(1.0, (m - k) * ps[i]))
        out[i] = run
    return out


def bh(ps: list[float]) -> list[float]:
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i], reverse=True)
    out = [1.0] * m
    run = 1.0
    for k, i in enumerate(order):
        rank = m - k
        run = min(run, ps[i] * m / rank)
        out[i] = min(1.0, run)
    return out


# ---------------------------------------------------------------- data
class Data:
    def __init__(self, work: str):
        import numpy as np
        import pyarrow.parquet as pq
        t = pq.read_table(f"{work}/trades.parquet")
        g = {c: t.column(c).to_numpy() for c in t.column_names}
        del t
        self.mid = g["mid"].astype(np.int64)
        self.slot = g["slot"].astype(np.int64)
        self.ts = g["ts"].astype(np.int64)
        self.isbuy = g["isbuy"].astype(np.int8)
        self.sol = g["sol"].astype(np.float64)
        self.tok = g["tok"].astype(np.float64)
        self.vs = g["vs"].astype(np.float64)
        self.vt = g["vt"].astype(np.float64)
        self.wid = g["wid"].astype(np.int64)
        self.sigh = g["sigh"].astype(np.uint64)
        self.iscr = g["iscr"].astype(np.int8)
        del g
        c = pq.read_table(f"{work}/coins.parquet").to_pandas().sort_values("mid").reset_index(drop=True)
        self.coins = c
        self.nc = len(c)
        self.off = np.searchsorted(self.mid, np.arange(self.nc + 1)).astype(np.int64)
        self.create_slot = c["create_slot"].to_numpy(np.int64)
        self.t0 = c["t0"].to_numpy(np.int64)
        self.cslot = c["complete_slot"].fillna(-1).to_numpy(np.int64)
        self.dev_buy = c["dev_buy"].to_numpy(np.float64)
        w = pq.read_table(f"{work}/wallets.parquet").to_pandas()
        self.nw = int(w["wid"].max()) + 1
        wmap = dict(zip(w["trader"], w["wid"], strict=True))
        self.cre_wid = np.array([wmap.get(x, -1) for x in c["creator"]], dtype=np.int64)
        del wmap, w
        b = pq.read_table(f"{work}/blocks.parquet")
        self.blk_slot = b.column("slot").to_numpy().astype(np.int64)
        self.blk_ts = np.maximum.accumulate(b.column("ts").to_numpy().astype(np.int64))
        self.slot_s = (self.blk_ts[-1] - self.blk_ts[0]) / (self.blk_slot[-1] - self.blk_slot[0])

    def utc_day(self, slots):
        import numpy as np

        from .trench72_kernels import slot_time
        return np.array([datetime.fromtimestamp(int(slot_time(self.blk_slot, self.blk_ts, int(s))), UTC)
                         .strftime("%Y-%m-%d") for s in slots])


def epoch_of(slots):
    return 1062 + (slots - START_SLOT) // EPOCH_LEN


class Sim:
    def __init__(self, d: Data, exits: dict[str, dict[str, Any]]):
        self.d, self.ex = d, exits
        self.evals = 0

    def run(self, cidx, ssig, exit_name: str, L: int = 5, size: float = 0.1):
        import numpy as np

        from .trench72_kernels import BUF, FEE, PRIO, sim_many
        d = self.d
        p = rule_params(self.ex[exit_name])
        r, e, x = sim_many(np.asarray(cidx, np.int64), np.asarray(ssig, np.int64), d.off, d.cslot, d.slot, d.vs,
                           d.vt, L, size, END_SLOT, d.blk_slot, d.blk_ts, *p, FEE, BUF, PRIO)
        self.evals += 1
        ok = ~np.isnan(r)
        return r[ok], e[ok], x[ok], np.asarray(cidx)[ok]


def split_check(d: Data, r, ent, segs=None) -> dict[str, Any]:
    """Early / held-out stats + per-epoch and per-UTC-day checks."""
    import numpy as np
    early, late = ent < SPLIT_SLOT, ent >= SPLIT_SLOT
    out: dict[str, Any] = {"early": describe(r[early]), "held": describe(r[late])}
    units: dict[str, Any] = {}
    if segs is None:
        ep = epoch_of(ent)
        for e in (1062, 1063, 1064):
            units[f"epoch{e}"] = describe(r[ep == e], boot=False)
    else:
        for name, m in segs.items():
            units[name] = describe(r[m], boot=False)
    days = d.utc_day(ent) if len(ent) else np.array([])
    for day in sorted(set(days.tolist())):
        units[f"day{day}"] = describe(r[days == day], boot=False)
    out["units"] = units
    bad = []
    for k, u in units.items():
        if k.startswith(("epoch", "seg")):
            if u["n"] < 50:
                bad.append(f"{k} n={u['n']}<50")
                continue
        elif u["n"] < 50:
            continue
        if u["mean"] <= 0 or (u.get("ex_top3") is not None and u["ex_top3"] <= 0):
            bad.append(f"{k} mean={u['mean']} ex_top3={u.get('ex_top3')}")
    out["unit_fail"] = bad
    return out


# ---------------------------------------------------------------- baselines
def baseline_b1(d: Data):
    from .trench72_kernels import b1_signals
    return b1_signals(d.off, d.slot, d.ts, d.t0, d.create_slot, 15, 5, d.blk_slot, d.blk_ts)


def baseline_b2(d: Data):
    import numpy as np

    from . import trench_hft as hft
    from .trench_fresh import FreshConfig, evaluate
    cfg = FreshConfig()
    oc, os_ = [], []
    max_slots45 = int(45 / d.slot_s) + 1
    for c in range(d.nc):
        lo, hi = int(d.off[c]), int(d.off[c + 1])
        if hi - lo < 5:
            continue
        cs = int(d.create_slot[c])
        lim = lo + int(np.searchsorted(d.slot[lo:hi], cs + max_slots45, side="right"))
        if lim - lo < 5:
            continue
        sl = d.slot[lo:lim]
        tr = [(float(d.t0[c] + (s - cs) * d.slot_s), bool(b), float(so), int(w), float(v / t * 1e6))
              for s, b, so, w, v, t in zip(sl, d.isbuy[lo:lim], d.sol[lo:lim], d.wid[lo:lim], d.vs[lo:lim],
                                           d.vt[lo:lim], strict=True)]
        coin = {"t0": float(d.t0[c]), "creator": int(d.cre_wid[c]), "tr": tr, "smart": set()}
        for age in (15, 45):
            f = hft.features(coin, age)
            dec, _ = evaluate(f, age, cfg)
            if dec == "enter":
                oc.append(c)
                os_.append(cs + int(age / d.slot_s))
                break
            if dec == "drop":
                break
    return np.array(oc, np.int64), np.array(os_, np.int64)


# ---------------------------------------------------------------- (a) copy wallets
def idea_a(d: Data, S: Sim, fam: list) -> dict[str, Any]:
    import numpy as np

    from .trench72_kernels import first_buys
    seen = np.full(d.nw, -1, np.int64)
    bc, bsl, bw = first_buys(d.off, d.slot, d.ts, d.isbuy, d.wid, d.iscr, d.cre_wid, d.t0, 120, seen)
    r, e, x, c2 = S.run(bc, bsl, "time30")
    # S.run drops non-executable rows; rebuild aligned arrays
    ok_idx = _aligned(bc, bsl, c2, e, 5)
    sw = bw[ok_idx]
    sc_ret, sc_exit = r, x
    segs = {"seg1062b": (E1062_SCORE_END, E1063), "disc1063": (E1063, SPLIT_SLOT), "held1064": (SPLIT_SLOT,
                                                                                               END_SLOT + 1)}
    res: dict[str, Any] = {"n_first_buys": len(bc), "n_scored": len(r), "variants": {}}

    def chosen(seg_start, min_n, min_t):
        m = sc_exit < seg_start
        w, rr = sw[m], sc_ret[m]
        order = np.argsort(w, kind="stable")
        w, rr = w[order], rr[order]
        uw, st, cnt = np.unique(w, return_index=True, return_counts=True)
        s1 = np.add.reduceat(rr, st) if len(st) else np.array([])
        s2 = np.add.reduceat(rr * rr, st) if len(st) else np.array([])
        mu = s1 / cnt
        var = np.where(cnt > 1, (s2 - cnt * mu * mu) / np.maximum(cnt - 1, 1), np.inf)
        tt = mu / np.sqrt(np.maximum(var, 1e-18) / cnt)
        return uw[(cnt >= min_n) & (tt >= min_t) & (mu > 0)]

    def signals(ws, lo, hi):
        m = np.isin(bw, ws) & (bsl >= lo) & (bsl < hi)
        cc, ss = bc[m], bsl[m]
        o = np.lexsort((ss, cc))
        cc, ss = cc[o], ss[o]
        first = np.ones(len(cc), bool)
        first[1:] = cc[1:] != cc[:-1]
        return cc[first], ss[first]

    for mn in (5, 10, 20):
        for mt in (1.0, 2.0, 3.0):
            for ex in ("time30", "time60", "trail20_t60", "ladder15_40_trail15_sl15_t60"):
                key = f"n{mn}_t{int(mt)}_{ex}"
                parts = {}
                for seg, (lo, hi) in segs.items():
                    ws = chosen(lo, mn, mt)
                    cc, ss = signals(ws, lo, hi)
                    rr, ee, _, _ = S.run(cc, ss, ex)
                    parts[seg] = (rr, ee, len(ws), cc, ss)
                disc = parts["disc1063"][0]
                p = tstat_p(disc)
                res["variants"][key] = {"wallets": {k: v[2] for k, v in parts.items()},
                                        "disc": describe(disc, boot=False),
                                        "seg1062b": describe(parts["seg1062b"][0], boot=False),
                                        "_parts": parts, "p": p}
                fam.append(("a", key, p))
    return res


def _aligned(cidx, ssig, c_ok, e_ok, L):
    """Indices of rows kept by Sim.run (ret not nan), in order."""
    import numpy as np
    # Sim.run preserves order, so match (cidx, ssig+L) sequentially
    idx = np.empty(len(c_ok), np.int64)
    j = 0
    for i in range(len(cidx)):
        if j < len(c_ok) and cidx[i] == c_ok[j] and ssig[i] + L == e_ok[j]:
            idx[j] = i
            j += 1
    assert j == len(c_ok)
    return idx


# ---------------------------------------------------------------- vetoes
def veto_eval(d: Data, r, e, vet) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, m in (("early", e < SPLIT_SLOT), ("held", e >= SPLIT_SLOT)):
        a, k = r[m & vet], r[m & ~vet]
        out[name] = {"n_all": int(m.sum()), "n_vetoed": len(a), "vetoed_mean": _mean(a), "kept": describe(k),
                     "all_mean": _mean(r[m]), "lift": _lift(k, r[m]), "welch_p": welch_less(a, k)}
    units = {}
    ep = epoch_of(e)
    days = d.utc_day(e)
    for nm, mm in [(f"epoch{x}", ep == x) for x in (1062, 1063, 1064)] + \
            [(f"day{x}", days == x) for x in sorted(set(days.tolist()))]:
        units[nm] = {"n": int(mm.sum()), "lift": _lift(r[mm & ~vet], r[mm]), "n_vetoed": int((mm & vet).sum())}
    out["units"] = units
    out["unit_fail"] = [k for k, u in units.items() if u["n"] >= 50 and (u["lift"] is None or u["lift"] <= 0)]
    return out


def _mean(x):
    return round(float(x.mean()), 5) if len(x) else None


def _lift(k, a):
    return round(float(k.mean() - a.mean()), 5) if len(k) and len(a) else None


def creator_hist(d: Data):
    """Per creator: sorted list of (t0, known_at, runner) for U coins."""
    from .trench72_kernels import max_prog_1h
    mp = max_prog_1h(d.off, d.ts, d.vs, d.t0)
    h: dict[str, list] = defaultdict(list)
    for c in range(d.nc):
        h[d.coins["creator"].iat[c]].append((int(d.t0[c]), int(d.t0[c]) + 3600, bool(mp[c] >= 0.5)))
    for v in h.values():
        v.sort()
    return h, mp


def originality(d: Data):
    """creator launched before in window; name+symbol seen before (all window launches, any quote)."""
    import duckdb
    rows = duckdb.sql(f"""select mint, creator, lower(trim(coalesce(name,'')))||'|'||lower(trim(coalesce(symbol,'')))
        k, create_slot from '{MD}/pumpfun/data/launches.parquet' where create_slot between {START_SLOT} and
        {END_SLOT} order by create_slot, mint""").fetchall()
    seen_c: dict[str, int] = defaultdict(int)
    seen_n: set[str] = set()
    feat = {}
    for mint, cr, k, _s in rows:
        feat[mint] = (seen_c[cr] if cr else 0, k in seen_n)
        if cr:
            seen_c[cr] += 1
        seen_n.add(k)
    return feat


# ---------------------------------------------------------------- (c2) PumpSwap
def idea_c2(d: Data, S: Sim, fam: list) -> dict[str, Any]:
    import duckdb
    import numpy as np

    from .trench72_kernels import BUF, FEE, PRIO, first_slot_at, sim_one
    pools = duckdb.sql(f"""select p.pool, p.mint, p.migrate_slot::bigint ms, epoch(p.migrate_time_utc)::bigint mt,
        epoch(p.amm_covered_until_utc)::bigint cov from '{MD}/pumpswap/data/pools.parquet' p
        where p.migrate_epoch = 1064 and p.quote_mint = '{SOL_MINT}'""").df()
    umints = set(d.coins["mint"])
    pools = pools[pools["mint"].isin(umints)].reset_index(drop=True)
    con = duckdb.connect()
    con.sql("set memory_limit='1500MB'; set threads=4; set preserve_insertion_order=false")
    pp = pools[["pool"]].reset_index().rename(columns={"index": "pid"})
    con.register("P", pp)
    tr = con.sql(f"""select P.pid::bigint pid, t.slot::bigint slot, epoch(t.block_time_utc)::bigint ts,
        t.eff_quote_reserves_after::double q, t.pool_base_reserves_after::double b
        from read_parquet('{MD}/pumpswap/data/trades/epoch=1064/*.parquet') t join P using (pool)
        where t.quote_mint = '{SOL_MINT}' and t.eff_quote_reserves_after > 0 and t.pool_base_reserves_after > 0
        order by pid, t.slot, t.tx_index, t.ev_idx""").fetchnumpy()
    con.close()
    pi = tr["pid"].astype(np.int64)
    off = np.searchsorted(pi, np.arange(len(pools) + 1)).astype(np.int64)
    slot, ts, q, b = (np.asarray(tr[c]) for c in ("slot", "ts", "q", "b"))
    slot = slot.astype(np.int64)
    ts = ts.astype(np.int64)
    q = q.astype(np.float64)
    b = b.astype(np.float64)
    del tr, pi
    early_cut = int(datetime(2026, 10, 22, tzinfo=UTC).timestamp())
    res: dict[str, Any] = {"pools": len(pools), "variants": {}}
    for delay in (0, 60, 300):
        for ex in ("time300", "time1800", "time21600", "trail20_t21600", "tp50_sl20_t21600"):
            p = rule_params(S.ex[ex])
            re, rl = [], []
            for i in range(len(pools)):
                lo, hi = int(off[i]), int(off[i + 1])
                if hi <= lo:
                    continue
                mt = int(pools["mt"].iat[i])
                k = lo + int(np.searchsorted(ts[lo:hi], mt + delay, side="left"))
                if k >= hi:
                    continue
                cov = pools["cov"].iat[i]
                end = END_SLOT
                if cov is not None and not math.isnan(cov):
                    cs = first_slot_at(d.blk_slot, d.blk_ts, int(cov))
                    end = min(END_SLOT, cs - 1) if cs > 0 else END_SLOT
                r, _, _ = sim_one(lo, hi, slot, q, b, int(slot[k]), 5, 0.1, -1, end, d.blk_slot, d.blk_ts,
                                  *p, FEE, BUF, PRIO)
                if not math.isnan(r):
                    (re if mt < early_cut else rl).append(r)
            S.evals += 1
            key = f"d{delay}_{ex}"
            pe = tstat_p(np.array(re))
            res["variants"][key] = {"early": describe(np.array(re), boot=False), "late": describe(np.array(rl)),
                                    "p": pe}
            fam.append(("c2", key, pe))
    return res


# ---------------------------------------------------------------- (h) META
def idea_h(d: Data, S: Sim, fam: list) -> dict[str, Any]:
    import duckdb
    import numpy as np

    from .trench72_kernels import first_slot_at
    pos = duckdb.sql(f"""select position_id, onchain_ret_open_to_close g, noema_realized_ret nr, verdict,
        coverage_ok, status, reason from '{MD}/meta_check/meta_positions_onchain.parquet'""").df()
    db = sqlite3.connect("file:/workspace/NOEMA/data/meta_launch.db?mode=ro", uri=True)
    size = {int(i): float(s or 0) for i, s in db.execute("select id, size_usd from meta_positions")}
    solusd = float(duckdb.sql(f"select median(close) from '{MD}/sol_usd/sol_usd_1m.parquet'").fetchall()[0][0]) \
        if _has_col(f"{MD}/sol_usd/sol_usd_1m.parquet", "close") else 104.0
    cov = pos[(pos["coverage_ok"]) & pos["g"].notna() & (pos["status"] == "closed")]
    net = []
    for _, row in cov.iterrows():
        usd = size.get(int(row["position_id"]), 5.0) or 5.0
        cost_frac = 2 * 0.0005 * solusd / usd
        net.append((1 + row["g"]) * (1 - 0.0125) ** 2 * 0.99 ** 2 - 1 - cost_frac)
    net = np.array(net)
    nr = cov["nr"].to_numpy(float)
    nr = nr[~np.isnan(nr)]
    h1 = {"n": len(net), "onchain_net": describe(net), "noema_realized": describe(nr),
          # labelled sensitivity (not the pre-registered test): drop positions NOEMA itself marked
          # unpriced (one carries a balance-delta price artifact of +537,497x)
          "onchain_net_excl_unpriced": describe(net[~cov["reason"].fillna("").str.contains("unpriced").to_numpy()]),
          "sol_usd": round(solusd, 2), "verdicts": cov["verdict"].value_counts().to_dict()}
    p1 = tstat_p(net)
    fam.append(("h", "h1_positions_onchain_net", p1))
    # h2
    mints = {m: i for i, m in enumerate(d.coins["mint"])}
    rows = db.execute("select mint, matched_at, rank, shield_ok from meta_matches").fetchall()
    db.close()
    cc, ss, sh, rk = [], [], [], []
    n_in = 0
    for mint, at, rank, ok in rows:
        if mint not in mints:
            continue
        t = datetime.fromisoformat(at).timestamp()
        s = first_slot_at(d.blk_slot, d.blk_ts, math.ceil(t))
        if s < 0:
            continue
        n_in += 1
        cc.append(mints[mint])
        ss.append(s)
        sh.append(int(ok or 0))
        rk.append(rank or "")
    cc, ss, sh, rk = np.array(cc, np.int64), np.array(ss, np.int64), np.array(sh), np.array(rk)
    r, e, _, c_ok = S.run(cc, ss, "time1800")
    idx = _aligned(cc, ss, c_ok, e, 5)
    shk, rkk = sh[idx], rk[idx]
    pas, fai = r[shk == 1], r[shk == 0]
    p2 = welch_less(fai, pas)
    fam.append(("h", "h2_shield_pass_vs_fail", p2))
    first = rkk == "first"
    h2 = {"matches_total": len(rows), "in_U_and_window": n_in, "executable_on_curve": len(r),
          "shield_pass": describe(pas), "shield_fail": describe(fai), "welch_p_fail_lt_pass": p2,
          "secondary_rank_first": {"pass": describe(r[first & (shk == 1)]), "fail": describe(r[first & (shk == 0)]),
                                   "welch_p": welch_less(r[first & (shk == 0)], r[first & (shk == 1)])}}
    return {"h1": h1, "h1_p": p1, "h2": h2}


def _has_col(path: str, col: str) -> bool:
    import duckdb
    return col in [r[0] for r in duckdb.sql(f"describe select * from '{path}'").fetchall()]


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:  # pragma: no cover - research driver
    import numpy as np
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/workspace/trench72_work")
    ap.add_argument("--out", default="docs/research/trench-72h-2026-10-23.json")
    a = ap.parse_args(argv)
    t_start = time.time()
    ex = _exits()
    lib30 = [k for k in ex if k in _exits_base()] + ["time300", "time600", "trail20_t300", "tp50_sl20_t600"]
    assert len(lib30) == 30, len(lib30)
    d = Data(a.work)
    S = Sim(d, ex)
    fam: list = []
    R: dict[str, Any] = {"data": {"coins_U": d.nc, "trades": len(d.slot), "slot_s": d.slot_s,
                                  "epochs": [1062, 1063, 1064]}}
    log = lambda *x: print(f"[{time.time() - t_start:7.1f}s]", *x, file=sys.stderr, flush=True)

    b1c, b1s = baseline_b1(d)
    b2c, b2s = baseline_b2(d)
    log("baselines", len(b1c), len(b2c))
    base = {"B1": (b1c, b1s, "time60"), "B2": (b2c, b2s, "fresh_g1_tp30_sl7.9_t120")}
    R["baselines"] = {}
    base_runs = {}
    for k, (c, s, exn) in base.items():
        r, e, x, cc = S.run(c, s, exn)
        base_runs[k] = (r, e, cc, _aligned(c, s, cc, e, 5))
        R["baselines"][k] = {"signals": len(c), "exit": exn, **split_check(d, r, e)}

    # (b) exits
    Rb = {}
    for k, (c, s, _) in base.items():
        for exn in lib30:
            r, e, _, _ = S.run(c, s, exn)
            p = tstat_p(r[e < SPLIT_SLOT])
            Rb[f"{k}|{exn}"] = {"early": describe(r[e < SPLIT_SLOT], boot=False), "p": p, "_re": (r, e)}
            fam.append(("b", f"{k}|{exn}", p))
    log("b done")
    # (c1)
    Rc1 = {}
    from .trench72_kernels import cross_signals
    for thr in (100, 150, 250, 350):
        c, s = cross_signals(d.off, d.slot, d.vs, d.vt, float(thr), d.cslot)
        for exn in ("time10", "time30", "time60", "trail10_t60", "trail20_t60", "tp20_sl10_t120",
                    "tp50_sl20_t120", "ladder15_40_trail15_sl15_t60"):
            r, e, _, _ = S.run(c, s, exn)
            p = tstat_p(r[e < SPLIT_SLOT])
            Rc1[f"mc{thr}|{exn}"] = {"signals": len(c), "early": describe(r[e < SPLIT_SLOT], boot=False),
                                     "p": p, "_re": (r, e), "_cs": (c, s, exn)}
            fam.append(("c1", f"mc{thr}|{exn}", p))
    log("c1 done")
    Rc2 = idea_c2(d, S, fam)
    log("c2 done")
    # vetoes d, e, f
    hist, _mp = creator_hist(d)
    orig = originality(d)
    from .trench72_kernels import manip_feats
    Rv: dict[str, Any] = {}
    for bk, (r, e, cc, idx) in base_runs.items():
        cre = d.coins["creator"].to_numpy()
        mints = d.coins["mint"].to_numpy()
        tent = np.array([int(_t) for _t in _slot_times(d, e)])
        known = np.zeros(len(r), np.int64)
        runners = np.zeros(len(r), np.int64)
        for i in range(len(r)):
            h = hist.get(cre[cc[i]], [])
            for t0, ka, ok in h:
                if t0 >= tent[i]:
                    break
                if ka <= tent[i]:
                    known[i] += 1
                    runners[i] += ok
        dv = {"veto_all_dead_ge1": (known >= 1) & (runners == 0), "veto_all_dead_ge2": (known >= 2) & (runners == 0),
              "keep_only_prior_runner": runners == 0}
        prior_n = np.array([orig.get(mints[c], (0, False))[0] for c in cc])
        name_seen = np.array([orig.get(mints[c], (0, False))[1] for c in cc])
        devb = d.dev_buy[cc] > 4.0
        ev = {"orcACR": (prior_n > 0) | name_seen | devb, "creator_prior": prior_n > 0, "name_seen": name_seen,
              "dev_buy_gt4": devb}
        sig = base[bk][1][idx]
        wash, atom, snip = manip_feats(cc, sig, d.off, d.slot, d.isbuy, d.tok, d.sol, d.wid, d.sigh, d.iscr,
                                       d.create_slot, d.cre_wid, np.full(d.nw, -2, np.int8), np.zeros(d.nw, np.int64),
                                       np.zeros(d.nw), np.zeros(d.nw, np.int64))
        fv = {}
        for w in (10, 25, 50):
            for at in (1, 3):
                fv[f"wash>{w}|atomic>={at}"] = (wash > w) | (atom >= at)
        for sp in (0.3, 0.5):
            fv[f"sniper_share>{sp}"] = snip > sp
        for idea, rules in (("d", dv), ("e", ev), ("f", fv)):
            for rn, vet in rules.items():
                key = f"{bk}|{rn}"
                res = veto_eval(d, r, e, vet)
                res["_vet"] = vet
                Rv.setdefault(idea, {})[key] = res
                fam.append((idea, key, res["early"]["welch_p"]))
    log("vetoes done")
    # (g)
    from .trench72_kernels import reentry_signals
    Rg = {}
    gsig = {}
    for mode, nm in ((0, "treatment"), (1, "matched_control"), (2, "no_inventory_control")):
        c, s = reentry_signals(d.off, d.slot, d.ts, d.isbuy, d.sol, d.tok, d.vs, d.wid, d.create_slot, d.t0,
                               d.cre_wid, mode, np.zeros(d.nw, np.int8), np.zeros(d.nw), np.zeros(d.nw),
                               np.zeros(d.nw, np.int64), np.zeros(d.nw, np.int64))
        r, e, _, _ = S.run(c, s, "reentry_tp40_sl20_t600_p95")
        gsig[nm] = (c, s, r, e)
        p = tstat_p(r[e < SPLIT_SLOT])
        Rg[nm] = {"signals": len(c), "p": p, **split_check(d, r, e)}
        fam.append(("g", nm, p))
    log("g done")
    Ra = idea_a(d, S, fam)
    log("a done")
    Rh = idea_h(d, S, fam)
    log("h done")
    assert len(fam) == N_FAMILY, len(fam)

    ps = [f[2] for f in fam]
    ho, bhv = holm(ps), bh(ps)
    adj = {(f[0], f[1]): {"p": f[2], "holm": ho[i], "bh": bhv[i], "bonf": min(1.0, f[2] * N_FAMILY)}
           for i, f in enumerate(fam)}
    R["family"] = {"n_tests": len(fam), "n_holm_lt_05": sum(1 for x in ho if x < 0.05),
                   "n_bh_lt_05": sum(1 for x in bhv if x < 0.05),
                   "min_p": min(ps), "top10": sorted(
                       [{"idea": f[0], "variant": f[1], "p": f[2], "holm": ho[i], "bh": bhv[i]}
                        for i, f in enumerate(fam)], key=lambda z: z["p"])[:10]}

    # ---- selection + held-out
    strat_sel = {}

    def pick(cands: dict[str, Any], getn, getm):
        ok = {k: v for k, v in cands.items() if getn(v) >= 100 and getm(v) is not None}
        if not ok:
            return None
        return max(ok, key=lambda k: getm(ok[k]))

    sel_b = pick(Rb, lambda v: v["early"]["n"], lambda v: v["early"]["mean"])
    sel_c1 = pick(Rc1, lambda v: v["early"]["n"], lambda v: v["early"]["mean"])
    sel_c2 = pick(Rc2["variants"], lambda v: v["early"]["n"], lambda v: v["early"]["mean"])
    sel_a = pick(Ra["variants"], lambda v: v["disc"]["n"], lambda v: v["disc"]["mean"])
    for idea, key, src in (("b", sel_b, Rb), ("c1", sel_c1, Rc1)):
        if key is None:
            strat_sel[idea] = {"selected": None}
            continue
        r, e = src[key]["_re"]
        sc = split_check(d, r, e)
        strat_sel[idea] = {"selected": key, **sc, "adj": adj[(idea, key)]}
    if sel_a:
        parts = Ra["variants"][sel_a]["_parts"]
        rr = np.concatenate([parts[k][0] for k in ("seg1062b", "disc1063", "held1064")])
        ee = np.concatenate([parts[k][1] for k in ("seg1062b", "disc1063", "held1064")])
        segm = {"seg1062b": (ee >= E1062_SCORE_END) & (ee < E1063 + 5), "seg_disc1063": (ee >= E1063 + 5) &
                (ee < SPLIT_SLOT), "seg_held1064": ee >= SPLIT_SLOT}
        sc = split_check(d, rr, ee, segs=segm)
        sc["early"] = describe(parts["disc1063"][0])
        sc["held"] = describe(parts["held1064"][0])
        strat_sel["a"] = {"selected": sel_a, **sc, "adj": adj[("a", sel_a)]}
    else:
        strat_sel["a"] = {"selected": None}
    strat_sel["c2"] = {"selected": sel_c2, **({"early": Rc2["variants"][sel_c2]["early"],
                                                "held": Rc2["variants"][sel_c2]["late"],
                                                "adj": adj[("c2", sel_c2)]} if sel_c2 else {})}
    strat_sel["g"] = {"selected": "treatment", "early": Rg["treatment"]["early"], "held": Rg["treatment"]["held"],
                      "units": Rg["treatment"]["units"], "unit_fail": Rg["treatment"]["unit_fail"],
                      "adj": adj[("g", "treatment")]}
    # g lifts
    tr_, mc_ = gsig["treatment"], gsig["matched_control"]
    glift = {}
    for nm, (lo, hi) in (("early", (0, SPLIT_SLOT)), ("held", (SPLIT_SLOT, 10**12))):
        a1 = tr_[2][(tr_[3] >= lo) & (tr_[3] < hi)]
        b1 = mc_[2][(mc_[3] >= lo) & (mc_[3] < hi)]
        glift[nm] = {"lift": _lift(a1, b1) if len(a1) and len(b1) else None, "welch_p": welch_less(b1, a1)}
    for ep_ in (1062, 1063, 1064):
        a1 = tr_[2][epoch_of(tr_[3]) == ep_]
        b1 = mc_[2][epoch_of(mc_[3]) == ep_]
        glift[f"epoch{ep_}"] = {"lift": (round(float(a1.mean() - b1.mean()), 5) if len(a1) and len(b1) else None)}
    strat_sel["g"]["lift_vs_matched"] = glift

    held_ps = {k: v["held"]["p"] for k, v in strat_sel.items() if v.get("selected") and v.get("held", {}).get("n")}
    hk = list(held_ps)
    hh = holm([held_ps[k] for k in hk])
    for k, x in zip(hk, hh, strict=True):
        strat_sel[k]["held_holm"] = x

    # L=10 / L=2 / $5 sensitivities for selected strategies
    for idea, v in strat_sel.items():
        if not v.get("selected") or idea in ("c2",):
            continue
        sens = {}
        for lab, L, size in (("L2", 2, 0.1), ("L10", 10, 0.1), ("usd5", 5, 0.048)):
            if idea == "b":
                bk, exn = v["selected"].split("|")
                c, s, _ = base[bk]
            elif idea == "c1":
                c, s, exn = Rc1[v["selected"]]["_cs"]
            elif idea == "g":
                c, s, exn = gsig["treatment"][0], gsig["treatment"][1], "reentry_tp40_sl20_t600_p95"
            else:  # (a): re-run the copy signals of every segment at the new latency / size
                pa = Ra["variants"][v["selected"]]["_parts"]
                c = np.concatenate([pa[k][3] for k in ("seg1062b", "disc1063", "held1064")])
                s = np.concatenate([pa[k][4] for k in ("seg1062b", "disc1063", "held1064")])
                exn = v["selected"].split("_", 2)[2]
            r, e, _, _ = S.run(c, s, exn, L=L, size=size)
            sens[lab] = {"held": describe(r[e >= SPLIT_SLOT], boot=False),
                         "early": describe(r[e < SPLIT_SLOT], boot=False)}
        v["sens"] = sens

    def strat_pass(idea, v):
        why = []
        if not v.get("selected"):
            return False, ["no variant with n>=100 on early"]
        ea, he = v["early"], v["held"]
        if not (ea["n"] >= 150 and (ea["mean"] or -1) > 0 and v["adj"]["holm"] < 0.05):
            why.append(f"early fails (n={ea['n']}, mean={ea['mean']}, holm={v['adj']['holm']:.3g})")
        if not (he.get("n", 0) >= 150 and (he.get("mean") or -1) > 0 and he.get("ci95", [-1])[0] > 0
                and (he.get("ex_top3") or -1) > 0 and v.get("held_holm", 1) < 0.05):
            why.append(f"held-out fails (n={he.get('n')}, mean={he.get('mean')}, ci={he.get('ci95')}, "
                       f"ex_top3={he.get('ex_top3')}, holm={v.get('held_holm', 1):.3g})")
        if v.get("unit_fail"):
            why.append("per-epoch/day fails: " + "; ".join(v["unit_fail"][:4]))
        if idea == "c2":
            why.append("pre-declared: one epoch of AMM data, cannot meet per-day rule")
        s10 = v.get("sens", {}).get("L10")
        if isinstance(s10, dict) and not ((s10["held"]["mean"] or -1) > 0):
            why.append(f"L=10 held-out mean {s10['held']['mean']}")
        if idea == "g":
            gl = v["lift_vs_matched"]
            if not all((gl[k]["lift"] or -1) > 0 and gl[k]["welch_p"] < 0.05 for k in ("early", "held")):
                why.append(f"lift vs matched control fails ({gl['early']}, {gl['held']})")
            if not all((gl[f'epoch{e_}']["lift"] or -1) > 0 for e_ in (1062, 1063, 1064)):
                why.append("lift not > 0 every epoch")
        return not why, why

    verdicts = {}
    for idea, v in strat_sel.items():
        ok, why = strat_pass(idea, v)
        verdicts[idea] = {"PASS": ok, "why": why}
    # vetoes: selection, held-out holm
    veto_sel = {}
    for idea in ("d", "e", "f"):
        cands = {k: v for k, v in Rv[idea].items() if v["early"]["n_vetoed"] >= 30}
        if not cands:
            veto_sel[idea] = None
            continue
        k = min(cands, key=lambda z: cands[z]["early"]["welch_p"])
        veto_sel[idea] = k
    vk = [i for i in veto_sel if veto_sel[i]]
    vh = holm([Rv[i][veto_sel[i]]["held"]["welch_p"] for i in vk])
    for i, x in zip(vk, vh, strict=True):
        v = Rv[i][veto_sel[i]]
        why = []
        if adj[(i, veto_sel[i])]["holm"] >= 0.05:
            why.append(f"early Holm p {adj[(i, veto_sel[i])]['holm']:.3g} >= 0.05 (raw {v['early']['welch_p']:.3g})")
        if not (x < 0.05 and (v["held"]["lift"] or -1) > 0):
            why.append(f"held-out Welch Holm p {x:.3g}, lift {v['held']['lift']}")
        if v["unit_fail"]:
            why.append("lift <= 0 in " + ", ".join(v["unit_fail"]))
        kept = v["held"]["kept"]
        desk_ok = (kept["n"] >= 150 and (kept["mean"] or -1) > 0 and kept.get("ci95", [-1])[0] > 0)
        verdicts[i] = {"PASS": not why, "selected": veto_sel[i], "held_holm": x, "why": why,
                       "kept_strategy_profitable_heldout": desk_ok}
    # scaling check for any pass (prereg section 7): sizes with own-impact fills + concurrency
    scaling = {}
    for i in ("d", "e", "f"):
        if not verdicts.get(i, {}).get("PASS"):
            continue
        bk, rn = veto_sel[i].split("|", 1)
        c, s_, exn = base[bk]
        _, _, cc0, idx0 = base_runs[bk]
        vet = Rv[i][veto_sel[i]]["_vet"]
        keep_c = cc0[~vet]
        keep_s = s_[idx0][~vet]
        rows = {}
        for size in (0.1, 0.5, 1.0, 2.0, 5.0, 10.0):
            r, e, x, _ = S.run(keep_c, keep_s, exn, size=size)
            rows[str(size)] = {"held": describe(r[e >= SPLIT_SLOT], boot=False),
                               "early": describe(r[e < SPLIT_SLOT], boot=False)}
        r, e, x, _ = S.run(keep_c, keep_s, exn)
        hours = (END_SLOT - START_SLOT) * d.slot_s / 3600
        order = np.argsort(e)
        ev = np.concatenate([np.stack([e, np.ones(len(e))], 1), np.stack([x, -np.ones(len(x))], 1)])
        ev = ev[np.lexsort((ev[:, 1], ev[:, 0]))]
        conc = int(np.max(np.cumsum(ev[:, 1]))) if len(ev) else 0
        del order
        scaling[i] = {"kept_signals_per_hour": round(len(keep_c) / hours, 2), "max_concurrent": conc,
                      "by_size_sol": rows}
    for i in ("d", "e", "f"):
        if not veto_sel[i]:
            verdicts[i] = {"PASS": False, "why": ["no veto with >=30 vetoed early trades"]}
    verdicts["h"] = {"PASS": False, "why": ["pre-declared: only epoch 1064 overlaps META matches"]}

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if not str(k).startswith("_")}
        if isinstance(o, list | tuple):
            return [strip(v) for v in o]
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        return o
    R.update({"b_all": Rb, "c1_all": Rc1, "c2": Rc2, "vetoes": Rv, "g": Rg, "a": Ra, "h": Rh,
              "selected": strat_sel, "veto_selected": veto_sel, "scaling": scaling, "verdicts": verdicts,
              "evaluations": S.evals, "runtime_s": round(time.time() - t_start, 1)})
    with open(a.out, "w") as fh:
        json.dump(strip(R), fh, indent=1, default=str)
    log("wrote", a.out)
    print(json.dumps(strip(verdicts), indent=1))
    return 0


def _exits_base() -> set[str]:
    from .trench_edges import EXITS
    return set(EXITS)


def _slot_times(d: Data, slots):
    from .trench72_kernels import slot_time
    return [slot_time(d.blk_slot, d.blk_ts, int(s)) for s in slots]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
