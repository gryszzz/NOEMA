"""Fresh-launch paper path: cheap tick pre-filter on brand-new pump.fun coins.

The main desks only see pools older than an hour with real liquidity, so every new
pump.fun launch is rejected there. This path scores launches at 15/45/120 s using the
TRENCH-HFT features (buy velocity, unique buyers, top-3 buyer share, first-second
snipers, dev sold, curve progress), drops coins with no momentum, and paper-enters
survivors on a dedicated FRESH desk with small fixed sizing and its own risk limits.

Research status: the thresholds were picked on earlier walk-forward folds only. The
held-out fold showed NO edge (see PR). This path exists to measure, not to earn.
PAPER ONLY: it never builds an executor and never reads signer material.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import trench_hft as hft
from .trench_desks import CostModel, DeskParams, Mark, PaperDeskEngine, TrenchDeskStore

FRESH_DESK_BASE = "FRESH"
PUMP_SUPPLY = 1_000_000_000.0


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class FreshConfig:
    enabled: bool = False
    interval_seconds: float = 10.0
    # pre-filter (chosen on walk-forward folds 1-3, validated on fold 4)
    min_uniq_buyers: int = 20
    max_top3_share: float = 0.5
    max_first1s_share: float = 0.5
    min_ofi: float = 0.2
    max_progress: float = 0.6
    min_ret_10s: float = 0.0
    max_drawdown_from_peak: float = -0.25
    # fast drop: no momentum by 45 s -> stop watching
    drop_min_buyer_rate: float = 0.1
    # risk (separate, conservative)
    bankroll_usd: float = 50.0
    notional_usd: float = 5.0
    max_open: int = 3
    max_entries_per_hour: int = 12
    daily_loss_cap_usd: float = 15.0
    take_profit_pct: float = 0.30
    stop_loss_pct: float = 0.079
    max_hold_seconds: float = 120.0
    stale_trade_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> FreshConfig:
        d = cls()
        return cls(
            enabled=os.getenv("NOEMA_FRESH_LAUNCH_ENABLED", "0") == "1",
            interval_seconds=_num("NOEMA_FRESH_INTERVAL_SECONDS", d.interval_seconds),
            min_uniq_buyers=int(_num("NOEMA_FRESH_MIN_UNIQ_BUYERS", d.min_uniq_buyers)),
            max_top3_share=_num("NOEMA_FRESH_MAX_TOP3_SHARE", d.max_top3_share),
            max_first1s_share=_num("NOEMA_FRESH_MAX_FIRST1S_SHARE", d.max_first1s_share),
            min_ofi=_num("NOEMA_FRESH_MIN_OFI", d.min_ofi),
            max_progress=_num("NOEMA_FRESH_MAX_PROGRESS", d.max_progress),
            min_ret_10s=_num("NOEMA_FRESH_MIN_RET_10S", d.min_ret_10s),
            bankroll_usd=_num("NOEMA_FRESH_BANKROLL_USD", d.bankroll_usd),
            notional_usd=min(10.0, _num("NOEMA_FRESH_NOTIONAL_USD", d.notional_usd)),
            max_open=int(_num("NOEMA_FRESH_MAX_OPEN", d.max_open)),
            max_entries_per_hour=int(_num("NOEMA_FRESH_MAX_ENTRIES_PER_HOUR", d.max_entries_per_hour)),
            daily_loss_cap_usd=_num("NOEMA_FRESH_DAILY_LOSS_CAP_USD", d.daily_loss_cap_usd),
            take_profit_pct=_num("NOEMA_FRESH_TAKE_PROFIT_PCT", d.take_profit_pct),
            max_hold_seconds=_num("NOEMA_FRESH_MAX_HOLD_SECONDS", d.max_hold_seconds),
        )


def evaluate(f: dict[str, float] | None, age: float, cfg: FreshConfig) -> tuple[str, list[str]]:
    """Return (decision, reasons): 'enter', 'skip' (re-check at next age) or 'drop'."""
    if f is None:
        return ("drop" if age >= 45 else "skip"), ["fewer than 5 trades"]
    if f["dev_sold"]:
        return "drop", ["dev sold"]
    if age >= 45 and (f["buyer_rate"] < cfg.drop_min_buyer_rate or f["ofi"] < 0):
        return "drop", [f"no momentum (buyers/s={f['buyer_rate']:.2f}, ofi={f['ofi']:.2f})"]
    reasons = []
    if f["uniq_buyers"] < cfg.min_uniq_buyers:
        reasons.append(f"unique buyers {int(f['uniq_buyers'])} < {cfg.min_uniq_buyers}")
    if f["top3_share"] > cfg.max_top3_share:
        reasons.append(f"top-3 buyer share {f['top3_share']:.2f} > {cfg.max_top3_share}")
    if f["first1s_share"] > cfg.max_first1s_share:
        reasons.append(f"first-second snipers {f['first1s_share']:.2f} > {cfg.max_first1s_share}")
    if f["ofi"] < cfg.min_ofi:
        reasons.append(f"order flow {f['ofi']:.2f} < {cfg.min_ofi}")
    if f["progress"] > cfg.max_progress:
        reasons.append(f"curve progress {f['progress']:.2f} > {cfg.max_progress}")
    if f["ret_10s"] < cfg.min_ret_10s:
        reasons.append(f"10s return {f['ret_10s']:.2%} < {cfg.min_ret_10s:.0%}")
    if f["dd_peak"] <= cfg.max_drawdown_from_peak:
        reasons.append(f"down {f['dd_peak']:.0%} from peak")
    if reasons:
        return "skip", reasons
    summary = (f"buyers={int(f['uniq_buyers'])} top3={f['top3_share']:.2f} "
               f"snipers={f['first1s_share']:.2f} ofi={f['ofi']:.2f} curve={f['progress']:.2f}")
    return "enter", [summary]


def load_recent(conn: sqlite3.Connection, now: datetime, window_s: float,
                extra_mints: list[str] = ()) -> dict[str, dict[str, Any]]:
    since = (now - timedelta(seconds=window_s)).isoformat()
    coins: dict[str, dict[str, Any]] = {}
    for mint, at, creator, symbol in conn.execute(
            "SELECT mint, received_at, creator, symbol FROM launch_stream WHERE received_at >= ? "
            "AND mint LIKE '%pump'", (since,)):
        coins[mint] = {"t0": hft._ts(at), "creator": creator, "symbol": symbol, "tr": [], "smart": set()}
    for mint in extra_mints:
        row = conn.execute("SELECT received_at, creator, symbol FROM launch_stream WHERE mint=?",
                           (mint,)).fetchone()
        if row and mint not in coins:
            coins[mint] = {"t0": hft._ts(row[0]), "creator": row[1], "symbol": row[2], "tr": [],
                           "smart": set()}
    if not coins:
        return coins
    marks = ",".join("?" for _ in coins)
    for mint, at, side, sol, trader, mc in conn.execute(
            f"SELECT mint, received_at, side, sol, trader, market_cap_sol FROM launch_trades "
            f"WHERE mint IN ({marks}) ORDER BY received_at", list(coins)):
        if mc and mc > 0 and side in ("buy", "sell"):
            coins[mint]["tr"].append((hft._ts(at), side == "buy", float(sol or 0), trader, float(mc)))
    return coins


def curve_mark(coin: dict[str, Any], sol_usd: float) -> Mark | None:
    if not coin["tr"]:
        return None
    mc = coin["tr"][-1][4]
    real_sol = max(0.0, hft.v_from_mc(mc) - 30.0)
    return Mark(mc * sol_usd / PUMP_SUPPLY, max(1.0, 2 * real_sol * sol_usd), "pump_curve")


def ensure_fresh_desk(store: TrenchDeskStore, cfg: FreshConfig) -> str:
    row = store.conn.execute(
        "SELECT desk_id FROM trench_desks WHERE base_name=? AND status='active'",
        (FRESH_DESK_BASE,)).fetchone()
    if row:
        return row[0]
    params = DeskParams(position_fraction=min(1.0, cfg.notional_usd / cfg.bankroll_usd),
                        core_fraction=0.0, take_profit_pct=cfg.take_profit_pct,
                        stop_loss_pct=cfg.stop_loss_pct, core_stop_pct=cfg.stop_loss_pct,
                        rebuy_dip_pct=0.99, max_hold_seconds=cfg.max_hold_seconds,
                        max_positions=cfg.max_open)
    desk_id = store.create_desk(base_name=FRESH_DESK_BASE, generation=1, params=params,
                                bankroll_usd=cfg.bankroll_usd)
    store.conn.commit()
    return desk_id


def _realized_today(store: TrenchDeskStore, desk_id: str, now: datetime) -> float:
    day = now.date().isoformat()
    row = store.conn.execute(
        "SELECT COALESCE(SUM(realized_pnl_usd),0) FROM trench_desk_trades WHERE desk_id=? AND ts>=?",
        (desk_id, day)).fetchone()
    return float(row[0] or 0)


def _entries_last_hour(store: TrenchDeskStore, desk_id: str, now: datetime) -> int:
    since = (now - timedelta(hours=1)).isoformat()
    return int(store.conn.execute(
        "SELECT COUNT(*) FROM trench_desk_events WHERE desk_id=? AND event='paper_entry' AND ts>=?",
        (desk_id, since)).fetchone()[0])


class FreshLaunchRunner:
    """Stateful between ticks: remembers which (mint, age) was evaluated and which coins were dropped."""

    def __init__(self, db_path: str, cfg: FreshConfig | None = None) -> None:
        self.db_path = db_path
        self.cfg = cfg or FreshConfig.from_env()
        self.seen: set[tuple[str, int]] = set()
        self.dropped: set[str] = set()
        self.entered: set[str] = set()
        self.ticks = 0

    def tick(self, *, sol_usd: float, now: datetime | None = None,
             stale: list[str] | None = None) -> dict[str, Any]:
        cfg = self.cfg
        now = now or datetime.now(UTC)
        store = TrenchDeskStore(self.db_path)
        summary: dict[str, Any] = {"launches_window": 0, "evaluated": 0, "prefilter_passed": 0,
                                   "dropped": 0, "entries": 0, "exits": 0, "blocked": None}
        try:
            desk_id = ensure_fresh_desk(store, cfg)
            engine = PaperDeskEngine(store, costs=CostModel.from_env(), executor=None)
            open_rows = store.open_positions(desk_id)
            coins = load_recent(store.conn, now, max(hft.AGES) + 40,
                                [r["mint"] for r in open_rows])
            summary["launches_window"] = len(coins)
            # 1) exits first, from the bonding-curve mark
            for pos in open_rows:
                coin = coins.get(pos["mint"])
                mark = curve_mark(coin, sol_usd) if coin else None
                held = (now - datetime.fromisoformat(pos["opened_at"])).total_seconds()
                if mark is None:
                    if held >= cfg.max_hold_seconds * 3:
                        mark = Mark(pos["last_mark_price"] or pos["bounce_entry_price"], 1e6, "last_mark")
                    else:
                        continue
                last_trade = coin["tr"][-1][0] if coin and coin["tr"] else 0
                entry = pos["bounce_entry_price"] or pos["core_entry_price"]
                ret = mark.price_usd / entry - 1 if entry else 0.0
                reason = ("take profit" if ret >= cfg.take_profit_pct
                          else "stop loss" if ret <= -cfg.stop_loss_pct
                          else "max hold reached" if held >= cfg.max_hold_seconds
                          else "no trades (stale curve)" if now.timestamp() - last_trade > cfg.stale_trade_seconds
                          else None)
                if reason:
                    engine._close_position(desk_id, pos, mark, reason)
                    store.event(desk_id, "fresh_exit", mint=pos["mint"], symbol=pos["symbol"],
                                reason=reason, ret=round(ret, 4), held_s=round(held, 1))
                    summary["exits"] += 1
            # 2) entry budget
            blocked = None
            if stale:
                blocked = "stale " + ", ".join(stale)
            elif _realized_today(store, desk_id, now) <= -cfg.daily_loss_cap_usd:
                blocked = f"daily loss cap ${cfg.daily_loss_cap_usd:.0f} hit"
            elif _entries_last_hour(store, desk_id, now) >= cfg.max_entries_per_hour:
                blocked = f"max {cfg.max_entries_per_hour} entries/hour"
            summary["blocked"] = blocked
            # 3) pre-filter at each checkpoint age
            for mint, coin in coins.items():
                if mint in self.dropped or mint in self.entered:
                    continue
                age = now.timestamp() - coin["t0"]
                due = [a for a in hft.AGES if a <= age < a + 30 and (mint, a) not in self.seen]
                if not due:
                    continue
                a = due[-1]
                self.seen.add((mint, a))
                f = hft.features(coin, a)
                decision, reasons = evaluate(f, a, self.cfg)
                if decision == "skip" and a == max(hft.AGES):
                    decision = "drop"
                summary["evaluated"] += 1
                if decision == "drop":
                    self.dropped.add(mint)
                    summary["dropped"] += 1
                if decision == "enter":
                    summary["prefilter_passed"] += 1
                    if blocked:
                        decision, reasons = "skip", [blocked, *reasons]
                mark = curve_mark(coin, sol_usd)
                if decision == "enter" and mark:
                    ok = engine.try_enter(desk_id, mint, coin.get("symbol") or mint[:6], mark,
                                          f"fresh-launch @{a}s: {reasons[0]}")
                    if ok:
                        self.entered.add(mint)
                        summary["entries"] += 1
                    else:
                        decision, reasons = "skip", ["desk full or no cash", *reasons]
                if decision == "drop" and f is None:
                    continue  # most launches never trade; keep the decision log readable
                store.event(desk_id, "fresh_decision", mint=mint, symbol=coin.get("symbol"),
                            age_s=a, decision=decision, reason="; ".join(reasons),
                            features={k: round(v, 4) for k, v in (f or {}).items()
                                      if k in ("uniq_buyers", "buyer_rate", "top3_share",
                                               "first1s_share", "ofi", "progress", "ret_10s")})
            self.ticks += 1
            if summary["entries"] or summary["exits"] or summary["prefilter_passed"] or self.ticks % 6 == 1:
                store.event(desk_id, "fresh_cycle", **summary)
            if self.ticks % 6 == 1:
                engine.void_suspended()
            engine.commit()
        finally:
            store.close()
        if len(self.seen) > 50_000:
            self.seen.clear()
            self.dropped = set(list(self.dropped)[-5000:])
        return summary
