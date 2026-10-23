"""Pre-registered confirmation test for ideas (f) and (g) of the 72 h trench study.

Pre-registration: docs/research/trench-fg-confirmation-prereg-2026-10-23.md (committed before any data from
epochs 1052-1061 was opened). Original study: docs/research/trench-72h-prereg-2026-10-23.md, noema/trench72.py,
noema/trench72_kernels.py. Every definition, parameter, fill model and control is imported from that code
unchanged; this module only re-points the data at new epochs and applies the new pass bar.

Research / paper only: reads parquet tapes, never trades, never touches wallets, keys or live flags.

    # Set A (epochs 1058-1061) once it has landed, then Set B (1052-1057), then the verdict:
    PYTHONPATH=. python -m noema.trench_fg_confirm prep --set A
    PYTHONPATH=. python -m noema.trench_fg_confirm run --set A
    PYTHONPATH=. python -m noema.trench_fg_confirm prep --set B
    PYTHONPATH=. python -m noema.trench_fg_confirm run --set B
    PYTHONPATH=. python -m noema.trench_fg_confirm verdict

Needs numpy, numba, scipy, pyarrow, duckdb, pandas (research venv, not NOEMA runtime deps). The pure-python
verdict logic (`verdict`, `holm`, `gapped_mask`, `check_set_ready`) has no third-party dependencies.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

from .trench72 import holm

EPOCH_LEN = 432_000
PF = "/workspace/marketdata/noema/pumpfun/data"
WORK = "/workspace/trench_fg_work"
OUT_DOC = "docs/research/trench-fg-confirmation-2026-10-23"
ALPHA = 0.05

# Confirmation sets (each evaluated on its own). "R" = the study's own epochs, used only to check that this
# module reproduces trench72.py on already-published data; it never enters the verdict.
SETS: dict[str, tuple[int, int]] = {"A": (1058, 1061), "B": (1052, 1057), "R": (1062, 1064)}
VERDICT_SETS = ("A", "B")
# Amendment 2026-10-23 (before any Set A data was read): side check, does not change the pass bar.
SIDE_EXCLUDE_EPOCH = {"A": 1061}

# ---- frozen definitions (copied from the study; do not change) -------------------------------------
L_PRIMARY = 5                # entry at signal slot + 5
SIZE_SOL = 0.1               # 0.1 SOL per trade
F_BASE = "B2"                # selected on the study's early split: B2|sniper_share>0.3
F_EXIT = "fresh_g1_tp30_sl7.9_t120"
F_SNIPER_SHARE = 0.3
G_EXIT = "reentry_tp40_sl20_t600_p95"
G_MODES = ((0, "treatment"), (1, "matched_control"), (2, "no_inventory_control"))
SOL_QUOTE = "11111111111111111111111111111111"


def epoch_bounds(first: int, last: int) -> tuple[int, int]:
    return first * EPOCH_LEN, (last + 1) * EPOCH_LEN - 1


def set_epochs(name: str) -> list[int]:
    a, b = SETS[name]
    return list(range(a, b + 1))


# ---------------------------------------------------------------- pure helpers (unit-tested)
def check_set_ready(name: str, pf: str = PF) -> list[str]:
    """Problems that block a run: every epoch of the set must be fully parsed, contiguous, and on disk."""
    probs = []
    prev_last = None
    for e in set_epochs(name):
        sp = f"{pf}/epoch-{e}.summary.json"
        if not os.path.exists(sp):
            probs.append(f"epoch {e}: no summary (not landed)")
            continue
        with open(sp) as fh:
            s = json.load(fh)
        lo, hi = epoch_bounds(e, e)
        if not s.get("parse_complete"):
            probs.append(f"epoch {e}: parse_complete is not true")
        if not os.path.isdir(f"{pf}/trades/epoch={e}") or not os.path.isdir(f"{pf}/blocks/epoch={e}"):
            probs.append(f"epoch {e}: trades/blocks partition missing")
        fs, ls = s.get("first_slot"), s.get("last_slot")
        if fs is None or ls is None or fs < lo or ls > hi:
            probs.append(f"epoch {e}: slots {fs}-{ls} outside {lo}-{hi}")
        if prev_last is not None and fs is not None and fs - prev_last > 1000:
            probs.append(f"epoch {e}: {fs - prev_last} slot hole after previous epoch")
        prev_last = ls
    return probs


def gapped_mask(create_slots: list[int], last_slots: list[int], gap_slots: list[int]) -> list[bool]:
    """A coin is 'gapped' if any archive gap record (log-truncated / multiframe-skipped / undecodable event,
    i.e. any row of events/epoch=N/gap.parquet) has a slot inside [create_slot, last trade slot]."""
    g = sorted(gap_slots)
    out = []
    for a, b in zip(create_slots, last_slots, strict=True):
        i = bisect.bisect_left(g, a)
        out.append(i < len(g) and g[i] <= b)
    return out


def _passes(t: dict[str, Any], key: str, adj: float) -> bool:
    lift = t.get(f"lift_{key}")
    return lift is not None and lift > 0 and adj < ALPHA


def verdict(res: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """res[set]['f'|'g'] -> {'p_all','lift_all','p_nogap','lift_nogap', g also 'kept_mean_all'}.

    Pass bar (pre-registered): each idea beats its control in Set A AND in Set B, each set on its own; Holm
    across the 4 tests (2 ideas x 2 sets) at alpha 0.05. Primary = all coins. The same 4 tests are re-done with
    gapped coins dropped (own Holm); an idea that passes only that way is 'needs fuller data', never PASS."""
    missing = [s for s in VERDICT_SETS if s not in res]
    if missing:
        return {"status": "PENDING", "missing_sets": missing}
    keys = [(i, s) for i in ("f", "g") for s in VERDICT_SETS]
    out: dict[str, Any] = {"tests": {}, "ideas": {}}
    adj: dict[str, dict[tuple[str, str], float]] = {}
    for scope in ("all", "nogap"):
        ps = [float(res[s][i][f"p_{scope}"]) for i, s in keys]
        adj[scope] = dict(zip(keys, holm(ps), strict=True))
    for i, s in keys:
        t = res[s][i]
        out["tests"][f"{i}|{s}"] = {
            "p": t["p_all"], "holm": adj["all"][(i, s)], "lift": t["lift_all"],
            "pass": _passes(t, "all", adj["all"][(i, s)]),
            "p_nogap": t["p_nogap"], "holm_nogap": adj["nogap"][(i, s)], "lift_nogap": t["lift_nogap"],
            "pass_nogap": _passes(t, "nogap", adj["nogap"][(i, s)])}
    out["side_checks"] = side_checks(res)
    for i in ("f", "g"):
        ok = all(out["tests"][f"{i}|{s}"]["pass"] for s in VERDICT_SETS)
        ok_ng = all(out["tests"][f"{i}|{s}"]["pass_nogap"] for s in VERDICT_SETS)
        status = "PASS" if ok else ("needs fuller data" if ok_ng else "FAIL")
        idea: dict[str, Any] = {"status": status}
        if i == "f":
            idea["role"] = "filter only"
            idea["desk_eligible"] = False
            idea["why_no_desk"] = "pre-registered: f is tested only as a filter; it never gets a desk"
        else:
            kept = {s: res[s]["g"].get("kept_mean_all") for s in VERDICT_SETS}
            positive = all(v is not None and v > 0 for v in kept.values())
            idea["kept_mean_by_set"] = kept
            idea["desk_eligible"] = bool(ok and positive)
            if not idea["desk_eligible"]:
                idea["why_no_desk"] = ("did not pass" if not ok else
                                       "mean net return of kept (treatment) trades not above 0 in every set")
        out["ideas"][i] = idea
    return out


def side_checks(res: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Amendment side check: Set A also reported with epoch 1061 excluded (1058-1060 only), because 1061 files
    were on disk before registration. Raw p-values only; NOT in the Holm family and NOT part of the pass bar."""
    out: dict[str, Any] = {}
    for s, ep in SIDE_EXCLUDE_EPOCH.items():
        cut = res.get(s, {}).get(f"excl_epoch{ep}")
        key = f"{s}_excl_{ep}"
        if cut is None:
            out[key] = {"status": "MISSING", "note": "set not run yet or run predates the side check"}
            continue
        a, b = SETS[s]
        out[key] = {"epochs": [e for e in range(a, b + 1) if e != ep], "affects_pass_bar": False,
                    **{i: {k: cut[i].get(k) for k in ("p_all", "lift_all", "kept_mean_all", "n_treat", "n_ctrl")
                           if k in cut[i]} for i in ("f", "g")}}
    return out


# ---------------------------------------------------------------- data prep (same SQL as the study's prep)
def prep(name: str, work: str = WORK, pf: str = PF) -> None:  # pragma: no cover - research data step
    import duckdb
    probs = check_set_ready(name, pf)
    if probs:
        raise SystemExit("set not ready: " + "; ".join(probs))
    eps = set_epochs(name)
    lo, hi = epoch_bounds(eps[0], eps[-1])
    wd = f"{work}/{name}"
    os.makedirs(f"{wd}/spill", exist_ok=True)
    elist = ",".join(str(e) for e in eps)
    tfiles = [f"{pf}/trades/epoch={e}/trades.parquet" for e in eps]
    bfiles = [f"{pf}/blocks/epoch={e}/blocks.parquet" for e in eps]
    c = duckdb.connect(f"{wd}/tmp.duckdb")
    c.sql(f"set memory_limit='3500MB'; set threads=6; set temp_directory='{wd}/spill'; "
          "set preserve_insertion_order=false")
    c.sql(f"""create or replace table U as select row_number() over (order by create_slot, mint) - 1 as mid,
     mint, creator, lower(trim(coalesce(name,'')))||'|'||lower(trim(coalesce(symbol,''))) as namekey,
     create_slot::bigint create_slot, epoch(create_time_utc)::bigint t0, coalesce(creator_initial_buy_sol,0)
     dev_buy, complete_slot::bigint complete_slot, migrate_slot::bigint migrate_slot, migration_pool
     from '{pf}/launches.parquet' where created_in_window and quote_mint='{SOL_QUOTE}'
     and coalesce(is_mayhem_mode,0)=0 and create_slot between {lo} and {hi}
     and init_virtual_sol_reserves=30000000000 and init_virtual_token_reserves=1073000000000000""")
    c.sql(f"""create or replace table T as select u.mid, t.slot::bigint slot, t.tx_index::int txi,
     t.ev_idx::int evi, epoch(t.block_time_utc)::bigint ts, (t.side='buy')::tinyint isbuy, t.sol_amount sol,
     t.token_amount tok, t.virtual_sol_reserves::double vs, t.virtual_token_reserves::double vt, t.trader,
     hash(t.signature) sigh, t.is_creator::tinyint iscr
     from read_parquet({tfiles!r}, hive_partitioning=1) t join U u using (mint) where t.epoch in ({elist})""")
    c.sql("create or replace table W as select trader, row_number() over () - 1 as wid from "
          "(select distinct trader from T)")
    c.sql(f"copy (select T.mid, slot, txi, evi, ts, isbuy, sol, tok, vs, vt, W.wid::int wid, sigh, iscr from T "
          f"join W using (trader) order by mid, slot, txi, evi) to '{wd}/trades.parquet' (compression zstd)")
    c.sql(f"copy U to '{wd}/coins.parquet'")
    c.sql(f"copy W to '{wd}/wallets.parquet'")
    c.sql(f"copy (select slot::bigint slot, epoch(block_time_utc)::bigint ts from read_parquet({bfiles!r}) "
          f"order by slot) to '{wd}/blocks.parquet'")
    gaps = []
    for e in eps:
        gp = f"{pf}/events/epoch={e}/gap.parquet"
        if os.path.exists(gp):
            gaps += [int(r[0]) for r in c.sql(f"select slot from '{gp}' where slot is not null").fetchall()]
    c.close()
    os.remove(f"{wd}/tmp.duckdb")
    with open(f"{wd}/gaps.json", "w") as fh:
        json.dump({"epochs": eps, "gap_slots": sorted(gaps)}, fh)
    print(f"prepped set {name} epochs {eps[0]}-{eps[-1]} -> {wd}", file=sys.stderr)


# ---------------------------------------------------------------- evaluation
def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=False).stdout.strip()
    except OSError:
        return ""


def run_set(name: str, work: str = WORK) -> dict[str, Any]:  # pragma: no cover - research driver
    import numpy as np

    from .trench72 import (
        Data,
        _aligned,
        _exits,
        _lift,
        _mean,
        baseline_b1,
        baseline_b2,
        describe,
        rule_params,
        welch_less,
    )
    from .trench72_kernels import BUF, FEE, PRIO, manip_feats, reentry_signals, sim_many
    eps = set_epochs(name)
    lo, end_slot = epoch_bounds(eps[0], eps[-1])
    wd = f"{work}/{name}"
    d = Data(wd)
    ex = _exits()
    with open(f"{wd}/gaps.json") as fh:
        gap_slots = json.load(fh)["gap_slots"]
    last_slot = np.array([int(d.slot[d.off[c + 1] - 1]) if d.off[c + 1] > d.off[c] else int(d.create_slot[c])
                          for c in range(d.nc)], np.int64)
    gapped = np.array(gapped_mask(d.create_slot.tolist(), last_slot.tolist(), gap_slots), bool)

    def run(cidx, ssig, exn):
        p = rule_params(ex[exn])
        r, e, x = sim_many(np.asarray(cidx, np.int64), np.asarray(ssig, np.int64), d.off, d.cslot, d.slot, d.vs,
                           d.vt, L_PRIMARY, SIZE_SOL, end_slot, d.blk_slot, d.blk_ts, *p, FEE, BUF, PRIO)
        ok = ~np.isnan(r)
        return r[ok], e[ok], x[ok], np.asarray(cidx)[ok]

    def epoch_of(slots):
        return np.asarray(slots) // EPOCH_LEN

    def units_of(e):
        ep = epoch_of(e)
        days = d.utc_day(e) if len(e) else np.array([])
        return ([(f"epoch{x}", ep == x) for x in eps] +
                [(f"day{x}", days == x) for x in sorted(set(days.tolist()))])

    out: dict[str, Any] = {"set": name, "epochs": eps, "slots": [lo, end_slot], "git_head": _git_head(),
                           "run_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                           "data": {"coins_U": d.nc, "trades": len(d.slot), "slot_s": d.slot_s,
                                    "gap_records": len(gap_slots), "gapped_coins": int(gapped.sum())}}

    # ---- (f) manipulation veto as a filter on its base strategy (control = the unfiltered base)
    bases = {"B1": (*baseline_b1(d), "time60"), "B2": (*baseline_b2(d), F_EXIT)}
    fcells: dict[str, Any] = {}
    fprim: dict[str, Any] = {}
    for bk, (c, s, exn) in bases.items():
        r, e, _, cc = run(c, s, exn)
        idx = _aligned(c, s, cc, e, L_PRIMARY)
        sig = s[idx]
        wash, atom, snip = manip_feats(cc, sig, d.off, d.slot, d.isbuy, d.tok, d.sol, d.wid, d.sigh, d.iscr,
                                       d.create_slot, d.cre_wid, np.full(d.nw, -2, np.int8),
                                       np.zeros(d.nw, np.int64), np.zeros(d.nw), np.zeros(d.nw, np.int64))
        fv = {}
        for w in (10, 25, 50):
            for at in (1, 3):
                fv[f"wash>{w}|atomic>={at}"] = (wash > w) | (atom >= at)
        for sp in (0.3, 0.5):
            fv[f"sniper_share>{sp}"] = snip > sp
        ng = ~gapped[cc]
        for rn, vet in fv.items():
            key = f"{bk}|{rn}"
            cell = {"n_all": len(r), "n_vetoed": int(vet.sum()), "vetoed_mean": _mean(r[vet]),
                    "kept": describe(r[~vet]), "all_mean": _mean(r), "lift": _lift(r[~vet], r),
                    "welch_p": welch_less(r[vet], r[~vet]),
                    "nogap": {"n_all": int(ng.sum()), "n_vetoed": int((vet & ng).sum()),
                              "lift": _lift(r[~vet & ng], r[ng]), "welch_p": welch_less(r[vet & ng], r[~vet & ng])},
                    "units": {u: {"n": int(m.sum()), "n_vetoed": int((m & vet).sum()),
                                  "kept_mean": _mean(r[m & ~vet]), "all_mean": _mean(r[m]),
                                  "lift": _lift(r[m & ~vet], r[m])} for u, m in units_of(e)}}
            fcells[key] = cell
            if bk == F_BASE and rn == f"sniper_share>{F_SNIPER_SHARE}":
                fprim = cell
                fprim_r, fprim_e, fprim_v = r, e, vet
    out["f_all_cells_descriptive"] = fcells
    out["f"] = {"cell": f"{F_BASE}|sniper_share>{F_SNIPER_SHARE}", "exit": F_EXIT,
                "p_all": fprim["welch_p"], "lift_all": fprim["lift"],
                "p_nogap": fprim["nogap"]["welch_p"], "lift_nogap": fprim["nogap"]["lift"],
                "kept_mean_all": fprim["kept"]["mean"], "detail": fprim}

    # ---- (g) re-entry after snipers exit vs matched control
    arms = {}
    for mode, nm in G_MODES:
        c, s = reentry_signals(d.off, d.slot, d.ts, d.isbuy, d.sol, d.tok, d.vs, d.wid, d.create_slot, d.t0,
                               d.cre_wid, mode, np.zeros(d.nw, np.int8), np.zeros(d.nw), np.zeros(d.nw),
                               np.zeros(d.nw, np.int64), np.zeros(d.nw, np.int64))
        r, e, _, cc = run(c, s, G_EXIT)
        arms[nm] = (r, e, cc)
    tr, mc = arms["treatment"], arms["matched_control"]
    tng, cng = ~gapped[tr[2]], ~gapped[mc[2]]
    gunits = {}
    for u, _m in units_of(np.concatenate([tr[1], mc[1]])):
        mt = dict(units_of(tr[1])).get(u, np.zeros(len(tr[0]), bool))
        mm = dict(units_of(mc[1])).get(u, np.zeros(len(mc[0]), bool))
        gunits[u] = {"treat": describe(tr[0][mt], boot=False), "control": describe(mc[0][mm], boot=False),
                     "lift": _lift(tr[0][mt], mc[0][mm])}
    out["g"] = {"exit": G_EXIT,
                "p_all": welch_less(mc[0], tr[0]), "lift_all": _lift(tr[0], mc[0]),
                "p_nogap": welch_less(mc[0][cng], tr[0][tng]), "lift_nogap": _lift(tr[0][tng], mc[0][cng]),
                "kept_mean_all": _mean(tr[0]),
                "arms": {nm: describe(v[0]) for nm, v in arms.items()},
                "arms_nogap": {"treatment": describe(tr[0][tng], boot=False),
                               "matched_control": describe(mc[0][cng], boot=False)},
                "units": gunits}

    # ---- amendment side check: same tests with one epoch's trades dropped (by entry slot); not in the pass bar
    if name in SIDE_EXCLUDE_EPOCH:
        xe = SIDE_EXCLUDE_EPOCH[name]
        fk = epoch_of(fprim_e) != xe
        fr, fv_ = fprim_r[fk], fprim_v[fk]
        tk, ck = epoch_of(tr[1]) != xe, epoch_of(mc[1]) != xe
        out[f"excl_epoch{xe}"] = {
            "f": {"p_all": welch_less(fr[fv_], fr[~fv_]), "lift_all": _lift(fr[~fv_], fr),
                  "kept_mean_all": _mean(fr[~fv_]), "n_treat": int((~fv_).sum()), "n_ctrl": len(fr)},
            "g": {"p_all": welch_less(mc[0][ck], tr[0][tk]), "lift_all": _lift(tr[0][tk], mc[0][ck]),
                  "kept_mean_all": _mean(tr[0][tk]), "n_treat": int(tk.sum()), "n_ctrl": int(ck.sum())}}
    return out


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["check", "prep", "run", "verdict"])
    ap.add_argument("--set", choices=sorted(SETS), default="A")
    ap.add_argument("--work", default=WORK)
    ap.add_argument("--out", default=OUT_DOC)
    a = ap.parse_args(argv)
    if a.cmd == "check":
        probs = check_set_ready(a.set)
        print(json.dumps({"set": a.set, "ready": not probs, "problems": probs}, indent=1))
        return 0 if not probs else 1
    if a.cmd == "prep":
        prep(a.set, a.work)
        return 0
    if a.cmd == "run":
        t0 = time.time()
        res = run_set(a.set, a.work)
        res["runtime_s"] = round(time.time() - t0, 1)
        path = f"{a.out}-set{a.set}.json"
        with open(path, "w") as fh:
            json.dump(res, fh, indent=1, default=float)
        print(json.dumps({k: res[k] for k in ("set", "data")} | {
            "f": {k: res["f"][k] for k in ("p_all", "lift_all", "p_nogap", "lift_nogap", "kept_mean_all")},
            "g": {k: res["g"][k] for k in ("p_all", "lift_all", "p_nogap", "lift_nogap", "kept_mean_all")}},
            indent=1, default=float))
        return 0
    res = {}
    for s in VERDICT_SETS:
        p = f"{a.out}-set{s}.json"
        if os.path.exists(p):
            with open(p) as fh:
                res[s] = json.load(fh)
    v = verdict(res)
    with open(f"{a.out}.json", "w") as fh:
        json.dump(v, fh, indent=1, default=float)
    print(json.dumps(v, indent=1, default=float))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
