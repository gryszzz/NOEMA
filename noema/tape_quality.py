"""Sanity rules for the pump.fun launch tape (launch_trades), shared by every research loader.

Why: the Helius TradeEvent decoder priced every curve as a standard SOL-quoted pump.fun curve
(market_cap_sol = virtual_sol / virtual_tokens * 1e9). pump.fun now also runs
  * mayhem-mode curves (TradeEvent.mayhem_mode = 1): virtual reserves are non-standard and move, so prints sit
    far below the 27.96 SOL floor of a standard curve and the fixed-K curve math of the research sims is wrong
    for them. One 0.0001 SOL buy can "move" them 100x+, which created the impossible 100-2000x returns;
  * non-SOL-quoted curves (TradeEvent.quote_mint != SOL): sol = 0 and virtual_sol = 0, so market_cap_sol = 0;
and PumpPortal also streams post-graduation AMM trades of coins that graduated in their create slot, at
"market caps" above the 410.9 SOL curve completion.

Every row and coin a loader drops is counted by reason (Stats), so a report can state what it excluded.
Rules (explicit, MQGB prereg 81b623de compatible):
  row  zero_sol      sol <= 0 or market_cap_sol <= 0 / NULL (non-SOL-quote curve, or a broken print)
  row  below_curve   market_cap_sol < 27.9  (a standard curve cannot print below 27.96 SOL)
  row  above_curve   market_cap_sol > 412   (the curve completes at ~410.9 SOL; above that is an AMM print)
  row  off_curve     sol and tokens inconsistent with the standard constant-product curve at that mcap (>5%)
  row  <ingest flag> quality column set by the decoder: mayhem / non_sol_quote / nonstd_curve
  coin nonstd_curve  any print of the coin hit below_curve / off_curve / an ingest flag -> whole coin dropped
                     (its prices are not comparable with the standard-curve sims)
  row  price_jump    an isolated print > 2x away from the previous kept print, more than its own SOL size can move
                     a standard curve, that the next print reverts (or any > 15x move: a single trade can move a
                     standard curve at most ~14.7x). PR 238's bad-tick precedent
  coin instant_grad  the coin reached >= 300 SOL in its create slot (or create mcap >= 300): bundled launch
  flag fast_grad     the coin reached the 410 SOL graduation within 60 s of its first trade (kept, flagged)
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from itertools import pairwise
from typing import Any

K = 30.0 * 1_073_000_000          # standard pump.fun curve: virtual SOL x virtual tokens (SOL x tokens)
CURVE_MIN_MC = 27.9               # 30^2 / K * 1e9 = 27.96 SOL, minus rounding
CURVE_MAX_MC = 412.0              # completion ~410.88 SOL (85 real SOL), plus rounding
GRAD_MC = 410.0
INSTANT_GRAD_MC = 300.0           # prereg rule: create-slot buys took the curve to >= 300 SOL
FAST_GRAD_S = 60.0
JUMP_MAX = 15.0                   # > max single-trade move on a standard curve (~14.7x)
JUMP_MIN = 2.0                    # smaller adjacent moves are never treated as bad ticks
JUMP_REVERT = 1.5                 # next print back within 1.5x of the pre-spike price -> isolated spike
CURVE_TOL = 0.05
CURVE_CHECK_MIN_SOL = 0.001       # tiny trades: tokens/mcap rounding dominates, skip the curve check
INGEST_FLAGS = ("mayhem", "non_sol_quote", "nonstd_curve")   # set by the decoder (exact)
# flag_history() also writes the heuristic reasons (nonstd_curve, instant_grad, zero_sol, above_curve, price_jump)
# into launch_trades.quality for rows captured before the decoder flagged them.
POISON = {"below_curve", "off_curve", *INGEST_FLAGS}
SOL_QUOTES = {None, "", "11111111111111111111111111111111", "So11111111111111111111111111111111111111112"}
STD_VIRTUAL_SOL_OFFSET = 30_000_000_000   # lamports: virtual_sol - real_sol on every standard curve


class Stats:
    """Per-reason exclusion counters (rows and coins) for a loader run."""

    def __init__(self) -> None:
        self.rows: Counter[str] = Counter()
        self.coins: Counter[str] = Counter()
        self.flags: Counter[str] = Counter()
        self.rows_in = self.rows_kept = self.coins_in = self.coins_kept = 0

    def as_dict(self) -> dict[str, Any]:
        return {"rows_in": self.rows_in, "rows_kept": self.rows_kept, "rows_excluded": dict(self.rows),
                "coins_in": self.coins_in, "coins_kept": self.coins_kept, "coins_excluded": dict(self.coins),
                "coins_flagged": dict(self.flags)}

    def summary(self) -> str:
        d = self.as_dict()
        return (f"tape_quality rows {d['rows_kept']}/{d['rows_in']} kept, excluded {d['rows_excluded']}; "
                f"coins {d['coins_kept']}/{d['coins_in']} kept, excluded {d['coins_excluded']}, "
                f"flagged {d['coins_flagged']}")


def curve_ratio(is_buy: bool, sol: float, tokens: float, mc: float) -> float | None:
    """tokens implied by the standard curve for this trade (post-trade mcap `mc`) / tokens reported. ~1 when the
    print is a standard SOL curve; far from 1 for mayhem / non-standard reserves. None when not checkable."""
    if not (sol and tokens and mc) or sol < CURVE_CHECK_MIN_SOL or tokens <= 0 or mc <= 0:
        return None
    va = math.sqrt(mc * K / 1e9)              # post-trade virtual SOL
    vb = va - sol if is_buy else va + sol     # pre-trade virtual SOL
    if vb <= 0:
        return math.inf
    return abs(K / vb - K / va) / tokens


def row_reason(side: Any, sol: Any, tokens: Any, mc: Any, quality: str | None = None) -> str | None:
    if quality:
        return quality
    if side not in ("buy", "sell"):
        return "bad_side"
    sol, mc = float(sol or 0), float(mc or 0)
    if sol <= 0 or mc <= 0:
        return "zero_sol"
    if mc < CURVE_MIN_MC:
        return "below_curve"
    if mc > CURVE_MAX_MC:
        return "above_curve"
    if tokens is not None:
        r = curve_ratio(side == "buy", sol, float(tokens or 0), mc)
        if r is not None and abs(r - 1) > CURVE_TOL:
            return "off_curve"
    return None


T, SIDE, SOL, TOK, MC, SLOT, QUAL, TRADER = range(8)   # Row tuple layout


def row(t: float, side: Any, sol: Any, tokens: Any, mc: Any, slot: Any = None, quality: Any = None,
        trader: Any = None) -> tuple:
    return (t, side, sol, tokens, mc, slot, quality, trader)


def curve_move_bound(prev_mc: float, sol: float, is_buy: bool) -> float:
    """Post-trade mcap a single trade of `sol` can reach from `prev_mc` on the standard curve."""
    v = math.sqrt(prev_mc * K / 1e9)
    v2 = v + sol if is_buy else max(v - sol, 1e-9)
    return v2 * v2 / K * 1e9


def is_spike(prev: tuple, cur: tuple, nxt: tuple | None) -> bool:
    """Bad tick (PR 238 precedent): `cur` moved > JUMP_MIN x from the previous kept print, further than its own trade
    size could move a standard curve, and the next print reverts to within JUMP_REVERT x of `prev` (or there is
    no next print). Also any adjacent move > JUMP_MAX (impossible on one standard-curve trade)."""
    p, m = float(prev[MC]), float(cur[MC])
    ratio = max(m / p, p / m)
    if ratio <= JUMP_MIN:
        return False
    reverts = nxt is None or max(float(nxt[MC]) / p, p / float(nxt[MC])) <= JUMP_REVERT
    if ratio > JUMP_MAX:
        return reverts
    sol = float(cur[SOL] or 0)
    if m > p:
        explained = m <= curve_move_bound(p, sol, True) * 1.05
    else:
        explained = m >= curve_move_bound(p, sol, False) / 1.05
    return reverts and not explained


def clean_coin(rows: Sequence[tuple], create_mc: float | None = None, stats: Stats | None = None
               ) -> tuple[list[tuple], str | None, set[str]]:
    """rows: one coin's time-ordered Row tuples (t, side, sol, tokens|None, mc, slot|None, quality|None, trader).
    Returns (kept rows, coin exclusion reason or None, flags). Counts every dropped row/coin into `stats`."""
    st = stats if stats is not None else Stats()
    st.coins_in += 1
    st.rows_in += len(rows)
    reasons = [row_reason(r[SIDE], r[SOL], r[TOK], r[MC], r[QUAL]) for r in rows]
    if any(x in POISON for x in reasons):
        st.coins["nonstd_curve"] += 1
        st.rows["nonstd_curve"] += len(rows)
        return [], "nonstd_curve", set()
    instant = (create_mc is not None and create_mc >= INSTANT_GRAD_MC) or any(x == "instant_grad" for x in reasons)
    first_slot = min((r[SLOT] for r in rows if r[SLOT] is not None), default=None)
    if not instant and first_slot is not None:
        instant = any(x is None and r[SLOT] == first_slot and float(r[MC]) >= INSTANT_GRAD_MC
                      for r, x in zip(rows, reasons, strict=True))
    if instant:
        st.coins["instant_grad"] += 1
        st.rows["instant_grad"] += len(rows)
        return [], "instant_grad", set()
    ok = []
    for r, x in zip(rows, reasons, strict=True):
        if x is None:
            ok.append(r)
        else:
            st.rows[x] += 1
    kept: list[tuple] = []
    for i, r in enumerate(ok):
        if kept and is_spike(kept[-1], r, ok[i + 1] if i + 1 < len(ok) else None):
            st.rows["price_jump"] += 1
            continue
        kept.append(r)
    flags: set[str] = set()
    if kept:
        t0 = float(kept[0][T])
        if any(float(r[MC]) >= GRAD_MC and float(r[T]) - t0 <= FAST_GRAD_S for r in kept):
            flags.add("fast_grad")
            st.flags["fast_grad"] += 1
    st.rows_kept += len(kept)
    if kept:
        st.coins_kept += 1
    else:
        st.coins["no_valid_rows"] += 1   # e.g. every print zero-SOL (non-SOL-quote curve before the decoder fix)
    return kept, None, flags


def create_mc_from_initial_sol(initial_sol: Any) -> float | None:
    """Standard-curve mcap right after the creator's create-tx buy of `initial_sol` SOL."""
    try:
        s = float(initial_sol)
    except (TypeError, ValueError):
        return None
    if s < 0:
        return None
    return (30.0 + s) ** 2 / K * 1e9


def launch_trade_cols(c: Any) -> set[str]:
    return {r[1] for r in c.execute("PRAGMA table_info(launch_trades)")}


def select_sql(c: Any, where: str = "WHERE mint LIKE '%pump'") -> str:
    """SELECT for launch_trades that tolerates research copies without the slot / quality columns.
    Columns: mint, received_at, side, sol, tokens, trader, market_cap_sol, slot, quality, source."""
    cols = launch_trade_cols(c)
    slot = "slot" if "slot" in cols else "NULL"
    q = "quality" if "quality" in cols else "NULL"
    src = "source" if "source" in cols else "NULL"
    return (f"SELECT mint, received_at, side, sol, tokens, trader, market_cap_sol, {slot}, {q}, {src} "
            f"FROM launch_trades {where} ORDER BY received_at")


def ingest_quality(mayhem: int | None, quote_mint: str | None, vsol: int | None, rsol: int | None) -> str | None:
    """Decoder-time flag from the full TradeEvent (exact, no heuristics)."""
    if quote_mint not in SOL_QUOTES:
        return "non_sol_quote"
    if mayhem:
        return "mayhem"
    if vsol is not None and rsol is not None and vsol - rsol != STD_VIRTUAL_SOL_OFFSET:
        return "nonstd_curve"
    return None


def backfill_row_reason(row: dict[str, Any]) -> str | None:
    """Same rules for the GOMD pump.fun backfill (raw/*.trades.tsv.gz), which carries the exact fields:
    mayhem_mode, quote_mint, virtual_sol_reserves, real_sol_reserves (lamports)."""
    def _i(k: str) -> int | None:
        v = row.get(k)
        try:
            return int(v) if v not in (None, "") else None
        except ValueError:
            return None
    q = ingest_quality(_i("mayhem_mode"), row.get("quote_mint") or None, _i("virtual_sol_reserves"),
                       _i("real_sol_reserves"))
    return q


# ------------------------------------------------------------------ manipulation (issue 2)
MCAP_LIQ_MAX = 200.0     # healthy graduated pump coins trade at mcap/liquidity ~2-30
MCAP_LIQ_MIN_MCAP_USD = 1_000_000.0


def mcap_unsupported(mcap_usd: Any, liquidity_usd: Any, max_ratio: float = MCAP_LIQ_MAX) -> bool:
    """True when a quoted market cap is far above what pool liquidity can support (wash/bundled 'runner'):
    mcap >= $1M with liquidity <= 0 or mcap/liquidity > max_ratio. Runner labels must exclude these."""
    try:
        m, liq = float(mcap_usd or 0), float(liquidity_usd or 0)
    except (TypeError, ValueError):
        return False
    if m < MCAP_LIQ_MIN_MCAP_USD:
        return False
    return liq <= 0 or m / liq > max_ratio


def coin_return_stats(coins: Iterable[Sequence[float]]) -> dict[str, float]:
    """max / p99 of (peak mcap / first mcap) and of adjacent-print returns across coins' mcap series."""
    peak, adj = [], []
    for mcs in coins:
        mcs = [m for m in mcs if m and m > 0]
        if len(mcs) < 2:
            continue
        peak.append(max(mcs) / mcs[0])
        adj.extend(b / a for a, b in pairwise(mcs))

    def q(xs: list[float], p: float) -> float:
        if not xs:
            return 0.0
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(p * len(xs)))]
    return {"coins": len(peak), "peak_max": max(peak, default=0.0), "peak_p99": q(peak, 0.99),
            "adj_max": max(adj, default=0.0), "adj_p99": q(adj, 0.99)}


# ------------------------------------------------------------------ CLI: flag history + report
def flag_history(db: str, batch: int = 2000, pause_s: float = 0.05) -> dict[str, int]:
    """Additive: write each excluded row's reason into launch_trades.quality (NULL rows only, never deletes or
    rewrites prices). Small batches so the live stream writer is never starved. Back the db up first."""
    import sqlite3
    import time

    from .launch_stream import connect
    c = connect(db)  # adds the quality column if missing
    c.execute("PRAGMA busy_timeout=30000")
    create_mc: dict[str, float | None] = {}
    for mint, ini, cmc in c.execute("SELECT mint, initial_sol, market_cap_sol FROM launch_stream WHERE mint LIKE '%pump'"):
        create_mc[mint] = float(cmc) if cmc else create_mc_from_initial_sol(ini) if ini else None
    from datetime import datetime
    raw: dict[str, list[tuple]] = {}
    sigs: dict[str, list[str]] = {}
    for sig, mint, at, side, sol, tok, trader, mc, slot, q in c.execute(
            "SELECT signature, mint, received_at, side, sol, tokens, trader, market_cap_sol, slot, quality "
            "FROM launch_trades WHERE mint LIKE '%pump' ORDER BY received_at"):
        raw.setdefault(mint, []).append(row(datetime.fromisoformat(at).timestamp(), side, sol, tok, mc, slot, q, trader))
        sigs.setdefault(mint, []).append(sig)
    upd: list[tuple[str, str]] = []
    for mint, rows in raw.items():
        kept, why, _f = clean_coin(rows, create_mc.get(mint), Stats())
        ss = sigs[mint]
        if why:
            upd.extend((why, sg) for sg, r in zip(ss, rows, strict=True) if not r[QUAL])
            continue
        keep = set(map(id, kept))
        for sg, r in zip(ss, rows, strict=True):
            if id(r) not in keep and not r[QUAL]:
                upd.append((row_reason(r[SIDE], r[SOL], r[TOK], r[MC]) or "price_jump", sg))
    n: Counter[str] = Counter()
    for i in range(0, len(upd), batch):
        part = upd[i:i + batch]
        for _try in range(20):
            try:
                c.executemany("UPDATE launch_trades SET quality=? WHERE signature=? AND quality IS NULL", part)
                c.commit()
                break
            except sqlite3.OperationalError:
                c.rollback()
                time.sleep(1)
        else:
            raise RuntimeError("noema.db stayed locked; rerun flag_history (it is idempotent)")
        n.update(x for x, _ in part)
        time.sleep(pause_s)
    c.close()
    return dict(n)


def report(db: str) -> dict[str, Any]:
    """Exclusions by reason and before/after max + p99 returns over the full research universe
    (trench_hft.load coins + trench_edges Helius-only coins), old filter vs these rules."""
    from . import trench_edges as te
    from . import trench_hft as hft

    def universe(quality: bool) -> dict[str, Any]:
        coins, _ = hft.load(db, None, quality=quality)
        coins.update(te._rpc_only(db, set(coins), quality=quality))
        return coins
    before = universe(False)
    b = coin_return_stats([x[4] for x in v["tr"]] for v in before.values())
    after = universe(True)
    a = coin_return_stats([x[4] for x in v["tr"]] for v in after.values())
    return {"quality": hft.QUALITY.as_dict(), "before": b, "after": a}


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m noema.tape_quality")
    ap.add_argument("cmd", choices=("report", "flag"))
    ap.add_argument("--db", default="data/noema.db")
    a = ap.parse_args(argv)
    out = report(a.db) if a.cmd == "report" else flag_history(a.db)
    print(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
