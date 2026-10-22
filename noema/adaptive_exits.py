"""ADAPTIVE EXITS for the paper alpha/culture book (paper only; never live).

Old: fixed TP ladder +30/+75/+150%, -15% hard stop, 24h time stop.
New:
  * partial take-profit at tp1 (default +30%, sell tp1_sell=50%), stop moves to break-even after it
  * keep a runner (runner fraction of the original size, 20-30%) under a volatility (ATR-like) trailing stop
  * time stop extended (by extend_h) only while momentum, volume and liquidity hold
  * early exits: liquidity pulled (>= liq_pull drop from its high), volume dried up, top-holder/dev selling,
    X stance flipped against the position
Exit params live in the agent genome (agent_lifecycle.DEFAULT_EXIT) so hire/fire can tune them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AdaptiveExit:
    tp1: float = 0.30
    tp1_sell: float = 0.5
    runner: float = 0.25
    trail_atr: float = 2.5
    min_trail: float = 0.08
    max_trail: float = 0.40
    stop: float = -0.15
    time_stop_h: float = 24.0
    extend_h: float = 24.0
    max_hold_h: float = 96.0
    liq_pull: float = 0.40
    vol_dry: float = 0.2

    @classmethod
    def from_genome(cls, g: dict[str, Any] | None) -> AdaptiveExit:
        base = cls()
        for k, v in (g or {}).items():
            if hasattr(base, k) and isinstance(v, (int, float)):
                setattr(base, k, float(v))
        return base

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "adaptive_v1", **asdict(self)}


@dataclass
class PosState:
    entry: float
    remaining: float = 1.0             # fraction of original size still held
    tp_hit: bool = False
    peak: float = 0.0
    liq_high: float = 0.0
    vol_high: float = 0.0
    rets: list[float] = field(default_factory=list)
    last_price: float = 0.0
    extended_h: float = 0.0


def vol_trail(st: PosState, p: AdaptiveExit) -> float:
    """ATR-like trail: trail_atr x mean absolute step return, clamped."""
    steps = [abs(r) for r in st.rets[-14:]]
    atr = sum(steps) / len(steps) if steps else p.min_trail / p.trail_atr
    return min(p.max_trail, max(p.min_trail, p.trail_atr * atr))


def decide(st: PosState, p: AdaptiveExit, *, price: float, age_h: float, liquidity: float | None = None,
           volume: float | None = None, holder_selling: bool = False, x_stance_flip: bool = False
           ) -> tuple[float, str | None]:
    """Return (fraction of ORIGINAL size to sell now, reason). Mutates st."""
    if st.last_price > 0:
        st.rets.append(price / st.last_price - 1)
    st.last_price = price
    st.peak = max(st.peak or st.entry, price)
    if liquidity:
        st.liq_high = max(st.liq_high, liquidity)
    if volume is not None:
        st.vol_high = max(st.vol_high, volume)
    ret = price / st.entry - 1

    def all_out(reason: str) -> tuple[float, str]:
        f = st.remaining
        st.remaining = 0.0
        return f, reason

    if liquidity and st.liq_high and liquidity <= st.liq_high * (1 - p.liq_pull):
        return all_out("liquidity pulled")
    if holder_selling:
        return all_out("top holder / dev selling")
    if x_stance_flip:
        return all_out("X stance flipped")
    if volume is not None and st.vol_high > 0 and volume <= st.vol_high * p.vol_dry and age_h >= 1:
        return all_out("volume dried up")
    if not st.tp_hit:
        if ret >= p.tp1:
            st.tp_hit = True
            sell = min(p.tp1_sell, max(0.0, st.remaining - p.runner))
            st.remaining -= sell
            return sell, "take profit 1 (stop -> break-even)"
        if ret <= p.stop:
            return all_out("hard stop")
    else:
        if price <= st.entry:
            return all_out("break-even stop")
        if price <= st.peak * (1 - vol_trail(st, p)):
            return all_out("volatility trailing stop")
        # second scale-out down to the runner once well in profit
        if st.remaining > p.runner + 1e-9 and ret >= 2.5 * p.tp1:
            sell = st.remaining - p.runner
            st.remaining = p.runner
            return sell, "scale to runner"
    limit = p.time_stop_h + st.extended_h
    if age_h >= limit:
        holding_up = (ret > 0 and (not st.rets or st.rets[-1] >= 0)
                      and (volume is None or st.vol_high == 0 or volume >= 0.5 * st.vol_high)
                      and (not liquidity or not st.liq_high or liquidity >= 0.8 * st.liq_high))
        if holding_up and limit + p.extend_h <= p.max_hold_h:
            st.extended_h += p.extend_h
            return 0.0, None
        return all_out("time stop")
    return 0.0, None


def old_rules_return(path: list[dict[str, float]], fee: float) -> float:
    """Old fixed rules: ladder (+30% sell 1/3, +75% sell 1/2, +150% all), -15% stop, 20% trail after TP1,
    time stop at the end of the path."""
    entry = path[0]["price"]
    rem, realized, hits, peak = 1.0, 0.0, 0, entry
    ladder = ((0.30, 1 / 3), (0.75, 1 / 2), (1.50, 1.0))
    for t in path[1:]:
        pr = t["price"]
        peak = max(peak, pr)
        r = pr / entry - 1
        if hits < 3 and r >= ladder[hits][0]:
            q = rem * ladder[hits][1]
            realized += q * (pr / entry)
            rem -= q
            hits += 1
        elif hits == 0 and r <= -0.15 or hits and pr <= peak * 0.8:
            realized += rem * pr / entry
            rem = 0
        if rem <= 1e-9:
            break
    realized += rem * path[-1]["price"] / entry
    return realized - 1 - fee


def new_rules_return(path: list[dict[str, float]], fee: float, p: AdaptiveExit | None = None) -> float:
    p = p or AdaptiveExit()
    entry = path[0]["price"]
    st = PosState(entry=entry, last_price=entry, liq_high=path[0].get("liq") or 0, vol_high=0)
    realized = 0.0
    for t in path[1:]:
        f, _ = decide(st, p, price=t["price"], age_h=t["h"], liquidity=t.get("liq"), volume=t.get("vol"))
        realized += f * t["price"] / entry
        if st.remaining <= 1e-9:
            break
    realized += st.remaining * path[-1]["price"] / entry
    return realized - 1 - fee


def load_paths(db: str, min_liq: float = 1000.0) -> list[list[dict[str, float]]]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        rows = c.execute("SELECT mint, horizon_seconds, tick_json FROM trench_observations ORDER BY mint, horizon_seconds")
        paths: dict[str, list[dict[str, float]]] = {}
        for mint, h, tj in rows:
            try:
                t = json.loads(tj)
                pr = float(t.get("price_usd") or 0)
            except (TypeError, ValueError):
                continue
            if pr <= 0:
                continue
            vol = (t.get("buy_volume_usd") or 0) + (t.get("sell_volume_usd") or 0)
            paths.setdefault(mint, []).append({"h": h / 3600, "price": pr, "liq": t.get("liquidity_usd") or 0,
                                               "vol": vol})
    finally:
        c.close()
    return [p for p in paths.values() if len(p) >= 4 and p[0]["liq"] >= min_liq]


def backtest(db: str, fee: float = 0.02, cap: float = 3.0) -> dict[str, Any]:
    from .edge_mining import bootstrap, mean
    paths = load_paths(db)
    if not paths:
        return {"status": "insufficient_data", "paths": 0}
    old = [min(cap, old_rules_return(p, fee)) for p in paths]
    new = [min(cap, new_rules_return(p, fee)) for p in paths]
    diff = [b - a for a, b in zip(old, new)]
    lo, hi, _ = bootstrap(diff)
    hold = [min(cap, p[-1]["price"] / p[0]["price"] - 1 - fee) for p in paths]
    return {"status": "ok", "paths": len(paths), "points_per_path": "30s,1m,2m,5m,15m,1h,6h (coarse)",
            "old_mean_net": round(mean(old), 4), "new_mean_net": round(mean(new), 4),
            "buy_and_hold_mean_net": round(mean(hold), 4),
            "new_minus_old_mean": round(mean(diff), 4), "new_minus_old_ci95": [round(lo, 4), round(hi, 4)],
            "new_better_share": round(sum(d > 0 for d in diff) / len(diff), 3),
            "note": "entries are every swept pool (no selection), so absolute returns are negative; the "
                    "comparison isolates exit quality. Paths only reach 6h, so time-stop logic is untested."}


FOLLOWUP_SCHEMA = """CREATE TABLE IF NOT EXISTS alpha_exit_paths (position_id INTEGER, chain TEXT, address TEXT,
  exit_at TEXT, exit_price REAL, reason TEXT, p1h REAL, p4h REAL, p24h REAL,
  PRIMARY KEY (position_id, exit_at))"""


def record_exit(conn: sqlite3.Connection, pid: int, chain: str, addr: str, at: str, price: float, reason: str) -> None:
    conn.execute(FOLLOWUP_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO alpha_exit_paths (position_id, chain, address, exit_at, exit_price, reason)"
                 " VALUES (?,?,?,?,?,?)", (pid, chain, addr, at, price, reason))


def fill_followups(conn: sqlite3.Connection, marks: dict[tuple[str, str], tuple[float, float]], now_iso: str) -> int:
    """Record the price +1h/+4h/+24h after each exit: sold-too-early vs held-too-long."""
    from datetime import datetime
    conn.execute(FOLLOWUP_SCHEMA)
    now = datetime.fromisoformat(now_iso)
    n = 0
    for pid, chain, addr, at, p1, p4, p24 in conn.execute(
            "SELECT position_id, chain, address, exit_at, p1h, p4h, p24h FROM alpha_exit_paths"
            " WHERE p24h IS NULL").fetchall():
        mk = marks.get((chain, addr))
        if not mk:
            continue
        age = (now - datetime.fromisoformat(at)).total_seconds() / 3600
        for col, h, cur in (("p1h", 1, p1), ("p4h", 4, p4), ("p24h", 24, p24)):
            if cur is None and age >= h:
                conn.execute(f"UPDATE alpha_exit_paths SET {col}=? WHERE position_id=? AND exit_at=?", (mk[0], pid, at))
                n += 1
    return n


def followup_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.execute(FOLLOWUP_SCHEMA)
    rows = conn.execute("SELECT exit_price, p24h, p4h FROM alpha_exit_paths WHERE exit_price>0").fetchall()
    later = [(r[1] or r[2]) / r[0] - 1 for r in rows if (r[1] or r[2])]
    return {"exits": len(rows), "with_followup": len(later),
            "sold_too_early": sum(v > 0.2 for v in later), "held_well": sum(v < -0.1 for v in later),
            "mean_move_after_exit": round(sum(later) / len(later), 4) if later else None}

