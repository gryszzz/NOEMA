"""Robustness rerun of trench-72h idea (c2), post-migration PumpSwap buy, restricted to pools with enough
post-migration AMM coverage. Research / paper only: reads parquet tapes, never trades, never touches keys.

Same definition, fill model, exits and split as `noema.trench72.idea_c2` (PR 264). The only change is a pool
filter on `hours_covered_post_migration >= 6` (and a second cut on `covered_24h`). Nothing is re-selected:
the variant reported is the one selected on the early split in PR 264 (d0_tp50_sl20_t21600); the matched
baseline is the passive hold with the same entry and fills (d0_time21600) on the same pools.

    python -m noema.trench72_c2_covered --work /workspace/trench72_work \
        --pools /workspace/trench72_c2cov/pools.parquet --out docs/research/trench-72h-c2-covered-rerun-2026-10-23.json
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .trench72 import END_SLOT, MD, SOL_MINT, Data, Sim, _exits, describe, rule_params

SELECTED = "d0_tp50_sl20_t21600"
BASELINE = "d0_time21600"
ET = timezone(timedelta(hours=-4))


def records(d: Data, S: Sim, pools_path: str) -> dict[str, list[dict[str, Any]]]:
    """Per-trade records for all 15 c2 variants, computed exactly as idea_c2 does."""
    import duckdb
    import numpy as np

    from .trench72_kernels import BUF, FEE, PRIO, first_slot_at, sim_one
    pools = duckdb.sql(f"""select p.pool, p.mint, p.migrate_slot::bigint ms, epoch(p.migrate_time_utc)::bigint mt,
        epoch(p.amm_covered_until_utc)::bigint cov, p.hours_covered_post_migration hcov, p.covered_24h c24
        from '{pools_path}' p
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
    slot, ts, q, b = slot.astype(np.int64), ts.astype(np.int64), q.astype(np.float64), b.astype(np.float64)
    del tr, pi
    early_cut = int(datetime(2026, 10, 22, tzinfo=UTC).timestamp())
    out: dict[str, list[dict[str, Any]]] = {}
    for delay in (0, 60, 300):
        for ex in ("time300", "time1800", "time21600", "trail20_t21600", "tp50_sl20_t21600"):
            p = rule_params(S.ex[ex])
            rows = []
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
                r, se, _ = sim_one(lo, hi, slot, q, b, int(slot[k]), 5, 0.1, -1, end, d.blk_slot, d.blk_ts,
                                   *p, FEE, BUF, PRIO)
                if not math.isnan(r):
                    h = pools["hcov"].iat[i]
                    rows.append({"mint": pools["mint"].iat[i], "mt": mt, "late": mt >= early_cut, "r": float(r),
                                 "se": int(se), "hcov": None if h is None or math.isnan(h) else float(h),
                                 "c24": bool(pools["c24"].iat[i])})
            out[f"d{delay}_{ex}"] = rows
    return out


def summarize(d: Data, rows: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np
    r = np.array([x["r"] for x in rows], dtype=float)
    o: dict[str, Any] = describe(r)
    if len(rows):
        utc = d.utc_day(np.array([x["se"] for x in rows]))
        et = np.array([datetime.fromtimestamp(x["mt"], ET).strftime("%Y-%m-%d") for x in rows])
        o["by_utc_day_entry"] = {k: describe(r[utc == k], boot=False) for k in sorted(set(utc.tolist()))}
        o["by_et_day_migration"] = {k: describe(r[et == k], boot=False) for k in sorted(set(et.tolist()))}
    return o


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - research driver
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/workspace/trench72_work")
    ap.add_argument("--pools", default=f"{MD}/pumpswap/data/pools.parquet")
    ap.add_argument("--out", default="docs/research/trench-72h-c2-covered-rerun-2026-10-23.json")
    a = ap.parse_args(argv)
    d = Data(a.work)
    S = Sim(d, _exits())
    R = records(d, S, a.pools)
    cuts = {"all (PR 264)": lambda x: True,
            "hcov>=6h": lambda x: x["hcov"] is not None and x["hcov"] >= 6,
            "covered_24h": lambda x: x["c24"]}
    res: dict[str, Any] = {"selected": SELECTED, "baseline": BASELINE, "pools_file": a.pools, "cuts": {}}
    for cname, f in cuts.items():
        c: dict[str, Any] = {}
        for v, rows in R.items():
            held = [x for x in rows if x["late"] and f(x)]
            early = [x for x in rows if not x["late"] and f(x)]
            if v in (SELECTED, BASELINE, "d0_time300", "d0_time1800"):
                c[v] = {"held": summarize(d, held), "early": describe([x["r"] for x in early], boot=False)}
            else:
                c[v] = {"held": describe([x["r"] for x in held], boot=False)}
        sel = {x["mint"] for x in R[SELECTED] if x["late"] and f(x)}
        base = {x["mint"]: x["r"] for x in R[BASELINE] if x["late"] and f(x)}
        both = sorted(sel & base.keys())
        sr = {x["mint"]: x["r"] for x in R[SELECTED] if x["late"]}
        import numpy as np
        diff = np.array([sr[m] - base[m] for m in both])
        c["paired_selected_minus_baseline"] = {"n": len(both), **({"mean_diff": round(float(diff.mean()), 5),
                                                                    "median_diff": round(float(np.median(diff)), 5)}
                                                                 if len(both) else {})}
        res["cuts"][cname] = c
    with open(a.out, "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    s = {k: {v: {kk: vv for kk, vv in c[v]["held"].items() if not kk.startswith("by_")}
             for v in (SELECTED, BASELINE)} for k, c in res["cuts"].items()}
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
