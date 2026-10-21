"""Paper-only parallel trench desks.

Each desk owns a paper bankroll slice, trades a held core leg plus a bounce leg, and is
quarantined ("fired") after consecutive losses, then replaced by a mutated successor.
Nothing in this module signs, quotes for execution, or submits a transaction: every fill
is simulated from an observed market quote with explicit fee and slippage estimates.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASE_DESK_NAMES = ("VET", "RISK", "FILLS", "TAPE", "FLOOR", "DEPTH", "DRIFT", "EDGE")
HARD_STOP_CEILING = 0.079  # every loser is cut before -8%


def _roman(n: int) -> str:
    table = ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    out = ""
    for value, glyph in table:
        while n >= value:
            out += glyph
            n -= value
    return out


def desk_display_name(base: str, generation: int) -> str:
    return base if generation <= 1 else f"{base} {_roman(generation)}"


@dataclass(frozen=True)
class DeskParams:
    position_fraction: float = 0.5
    core_fraction: float = 0.10
    take_profit_pct: float = 0.15
    stop_loss_pct: float = 0.05
    core_stop_pct: float = 0.079
    rebuy_dip_pct: float = 0.06
    max_hold_seconds: float = 6 * 3600.0
    max_positions: int = 2

    def validate(self) -> None:
        if not 0 < self.position_fraction <= 1:
            raise ValueError("position_fraction must be in (0, 1]")
        if not 0 <= self.core_fraction < 1:
            raise ValueError("core_fraction must be in [0, 1)")
        if not 0 < self.stop_loss_pct <= HARD_STOP_CEILING:
            raise ValueError("stop loss must be positive and tighter than 8%")
        if not 0 < self.core_stop_pct <= HARD_STOP_CEILING:
            raise ValueError("core stop must be positive and tighter than 8%")
        if self.take_profit_pct <= 0 or self.rebuy_dip_pct <= 0 or self.max_hold_seconds <= 0:
            raise ValueError("take profit, rebuy dip and hold time must be positive")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least 1")


BASE_DESK_PARAMS: tuple[DeskParams, ...] = (
    DeskParams(take_profit_pct=0.12, stop_loss_pct=0.04, rebuy_dip_pct=0.05),
    DeskParams(take_profit_pct=0.20, stop_loss_pct=0.06, rebuy_dip_pct=0.08,
               max_hold_seconds=8 * 3600.0),
    DeskParams(take_profit_pct=0.08, stop_loss_pct=0.03, rebuy_dip_pct=0.04,
               max_hold_seconds=3 * 3600.0, max_positions=3, position_fraction=0.33),
    DeskParams(take_profit_pct=0.30, stop_loss_pct=0.07, rebuy_dip_pct=0.10,
               core_fraction=0.15, max_hold_seconds=10 * 3600.0, max_positions=1,
               position_fraction=0.8),
    DeskParams(take_profit_pct=0.15, stop_loss_pct=0.05, rebuy_dip_pct=0.06,
               core_fraction=0.05),
)


def mutate_params(params: DeskParams, seed: str) -> DeskParams:
    rng = random.Random(seed)

    def jitter(value: float, low: float, high: float) -> float:
        return round(min(high, max(low, value * rng.uniform(0.75, 1.3))), 4)

    mutated = replace(
        params,
        take_profit_pct=jitter(params.take_profit_pct, 0.04, 0.6),
        stop_loss_pct=jitter(params.stop_loss_pct, 0.015, HARD_STOP_CEILING),
        rebuy_dip_pct=jitter(params.rebuy_dip_pct, 0.02, 0.2),
        core_fraction=jitter(max(params.core_fraction, 0.02), 0.0, 0.25),
        max_hold_seconds=jitter(params.max_hold_seconds, 1800.0, 24 * 3600.0),
    )
    mutated.validate()
    return mutated


@dataclass(frozen=True)
class CostModel:
    """Conservative paper execution costs."""

    swap_fee_bps: float = 30.0
    base_slippage_bps: float = 50.0
    network_fee_usd: float = 0.002

    @classmethod
    def from_env(cls) -> CostModel:
        return cls(
            swap_fee_bps=float(os.getenv("NOEMA_DESK_SWAP_FEE_BPS", "30")),
            base_slippage_bps=float(os.getenv("NOEMA_DESK_BASE_SLIPPAGE_BPS", "50")),
            network_fee_usd=float(os.getenv("NOEMA_DESK_NETWORK_FEE_USD", "0.002")),
        )

    def slippage_fraction(self, notional_usd: float, liquidity_usd: float) -> float:
        # Constant-product impact approximation: ~2 * size / pool liquidity.
        impact = 2.0 * notional_usd / max(liquidity_usd, 1.0)
        return min(0.5, self.base_slippage_bps / 10_000.0 + impact)

    def fill(self, *, side: str, quote_price: float, notional_usd: float,
             liquidity_usd: float) -> tuple[float, float, float]:
        """Return (fill_price, fee_usd, slippage_usd) for a simulated swap."""

        if quote_price <= 0 or notional_usd <= 0:
            raise ValueError("quote price and notional must be positive")
        slip = self.slippage_fraction(notional_usd, liquidity_usd)
        fill_price = quote_price * (1 + slip) if side == "buy" else quote_price * (1 - slip)
        fee = notional_usd * self.swap_fee_bps / 10_000.0 + self.network_fee_usd
        return fill_price, fee, notional_usd * slip


@dataclass(frozen=True)
class Mark:
    price_usd: float
    liquidity_usd: float
    source: str


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS trench_desks (
        desk_id TEXT PRIMARY KEY, name TEXT NOT NULL, base_name TEXT NOT NULL,
        generation INTEGER NOT NULL, status TEXT NOT NULL, params_json TEXT NOT NULL,
        starting_bankroll_usd REAL NOT NULL, cash_usd REAL NOT NULL,
        realized_pnl_usd REAL NOT NULL DEFAULT 0, fees_usd REAL NOT NULL DEFAULT 0,
        slippage_usd REAL NOT NULL DEFAULT 0, consecutive_losses INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        parent_desk_id TEXT, created_at TEXT NOT NULL, fired_at TEXT, fired_reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS trench_desk_positions (
        desk_id TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT NOT NULL,
        opened_at TEXT NOT NULL, status TEXT NOT NULL,
        core_qty REAL NOT NULL DEFAULT 0, core_cost_usd REAL NOT NULL DEFAULT 0,
        core_entry_price REAL, bounce_qty REAL NOT NULL DEFAULT 0,
        bounce_cost_usd REAL NOT NULL DEFAULT 0, bounce_entry_price REAL,
        bounce_budget_usd REAL NOT NULL DEFAULT 0, last_exit_price REAL,
        last_mark_price REAL, last_mark_at TEXT, closed_at TEXT,
        PRIMARY KEY (desk_id, mint, opened_at)
    )""",
    """CREATE TABLE IF NOT EXISTS trench_desk_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, desk_id TEXT NOT NULL, mint TEXT NOT NULL,
        symbol TEXT NOT NULL, leg TEXT NOT NULL, side TEXT NOT NULL, ts TEXT NOT NULL,
        quote_price REAL NOT NULL, quote_source TEXT NOT NULL, fill_price REAL NOT NULL,
        qty REAL NOT NULL, notional_usd REAL NOT NULL, fee_usd REAL NOT NULL,
        slippage_usd REAL NOT NULL, realized_pnl_usd REAL, reason TEXT NOT NULL,
        paper INTEGER NOT NULL DEFAULT 1 CHECK (paper = 1)
    )""",
    """CREATE TABLE IF NOT EXISTS trench_desk_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, desk_id TEXT,
        event TEXT NOT NULL, payload_json TEXT NOT NULL
    )""",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TrenchDeskStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        for statement in SCHEMA:
            self.conn.execute(statement)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def event(self, desk_id: str | None, event: str, **payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO trench_desk_events (ts, desk_id, event, payload_json) VALUES (?,?,?,?)",
            (_now(), desk_id, event, json.dumps(payload, sort_keys=True, default=str)),
        )

    def active_desks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM trench_desks WHERE status='active' ORDER BY created_at, desk_id"
        ))

    def all_desks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM trench_desks ORDER BY created_at, desk_id"))

    def create_desk(self, *, base_name: str, generation: int, params: DeskParams,
                    bankroll_usd: float, parent_desk_id: str | None = None) -> str:
        params.validate()
        desk_id = f"{base_name.lower()}-g{generation}"
        self.conn.execute(
            """INSERT INTO trench_desks (desk_id, name, base_name, generation, status,
               params_json, starting_bankroll_usd, cash_usd, parent_desk_id, created_at)
               VALUES (?,?,?,?, 'active', ?,?,?,?,?)""",
            (desk_id, desk_display_name(base_name, generation), base_name, generation,
             json.dumps(asdict(params), sort_keys=True), bankroll_usd, bankroll_usd,
             parent_desk_id, _now()),
        )
        self.event(desk_id, "desk_hired", name=desk_display_name(base_name, generation),
                   params=asdict(params), bankroll_usd=bankroll_usd, parent=parent_desk_id)
        return desk_id

    def ensure_desks(self, count: int, total_bankroll_usd: float) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM trench_desks").fetchone()[0]:
            return
        if count < 1 or count > len(BASE_DESK_NAMES):
            raise ValueError("desk count out of range")
        slice_usd = total_bankroll_usd / count
        for index in range(count):
            params = BASE_DESK_PARAMS[index % len(BASE_DESK_PARAMS)]
            self.create_desk(base_name=BASE_DESK_NAMES[index], generation=1,
                             params=params, bankroll_usd=slice_usd)
        self.conn.commit()

    def open_positions(self, desk_id: str | None = None) -> list[sqlite3.Row]:
        if desk_id is None:
            return list(self.conn.execute(
                "SELECT * FROM trench_desk_positions WHERE status='open' ORDER BY opened_at"))
        return list(self.conn.execute(
            "SELECT * FROM trench_desk_positions WHERE status='open' AND desk_id=?", (desk_id,)))

    def record_trade(self, **fields: Any) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.conn.execute(f"INSERT INTO trench_desk_trades ({cols}) VALUES ({marks})",
                          tuple(fields.values()))


def desk_params(row: sqlite3.Row) -> DeskParams:
    return DeskParams(**json.loads(row["params_json"]))


class PaperDeskEngine:
    """Deterministic paper desk state machine. No network, no signing."""

    def __init__(self, store: TrenchDeskStore, *, costs: CostModel | None = None,
                 fire_after_losses: int = 2) -> None:
        self.store = store
        self.costs = costs or CostModel()
        self.fire_after_losses = fire_after_losses

    # -- low level legs -------------------------------------------------------
    def _buy(self, desk: sqlite3.Row, mint: str, symbol: str, leg: str, notional: float,
             mark: Mark, reason: str) -> tuple[float, float]:
        fill_price, fee, slip = self.costs.fill(side="buy", quote_price=mark.price_usd,
                                                notional_usd=notional,
                                                liquidity_usd=mark.liquidity_usd)
        qty = (notional - fee) / fill_price
        self.store.record_trade(
            desk_id=desk["desk_id"], mint=mint, symbol=symbol, leg=leg, side="buy", ts=_now(),
            quote_price=mark.price_usd, quote_source=mark.source, fill_price=fill_price,
            qty=qty, notional_usd=notional, fee_usd=fee, slippage_usd=slip,
            realized_pnl_usd=None, reason=reason,
        )
        self.store.conn.execute(
            """UPDATE trench_desks SET cash_usd=cash_usd-?, fees_usd=fees_usd+?,
               slippage_usd=slippage_usd+? WHERE desk_id=?""",
            (notional, fee, slip, desk["desk_id"]))
        return qty, fill_price

    def _sell(self, desk: sqlite3.Row, mint: str, symbol: str, leg: str, qty: float,
              cost_usd: float, mark: Mark, reason: str) -> float:
        gross = qty * mark.price_usd
        fill_price, fee, slip = self.costs.fill(side="sell", quote_price=mark.price_usd,
                                                notional_usd=gross,
                                                liquidity_usd=mark.liquidity_usd)
        proceeds = qty * fill_price - fee
        pnl = proceeds - cost_usd
        self.store.record_trade(
            desk_id=desk["desk_id"], mint=mint, symbol=symbol, leg=leg, side="sell", ts=_now(),
            quote_price=mark.price_usd, quote_source=mark.source, fill_price=fill_price,
            qty=qty, notional_usd=proceeds, fee_usd=fee, slippage_usd=slip,
            realized_pnl_usd=pnl, reason=reason,
        )
        win = pnl > 0
        self.store.conn.execute(
            f"""UPDATE trench_desks SET cash_usd=cash_usd+?, fees_usd=fees_usd+?,
                slippage_usd=slippage_usd+?, realized_pnl_usd=realized_pnl_usd+?,
                {'wins=wins+1, consecutive_losses=0' if win else
                 'losses=losses+1, consecutive_losses=consecutive_losses+1'}
                WHERE desk_id=?""",
            (proceeds, fee, slip, pnl, desk["desk_id"]))
        return pnl

    def _desk(self, desk_id: str) -> sqlite3.Row:
        return self.store.conn.execute(
            "SELECT * FROM trench_desks WHERE desk_id=?", (desk_id,)).fetchone()

    def _close_position(self, desk_id: str, pos: sqlite3.Row, mark: Mark, reason: str) -> None:
        desk = self._desk(desk_id)
        if pos["bounce_qty"] > 0:
            self._sell(desk, pos["mint"], pos["symbol"], "bounce", pos["bounce_qty"],
                       pos["bounce_cost_usd"], mark, reason)
        desk = self._desk(desk_id)
        if pos["core_qty"] > 0:
            self._sell(desk, pos["mint"], pos["symbol"], "core", pos["core_qty"],
                       pos["core_cost_usd"], mark, reason)
        unused = pos["bounce_budget_usd"] if pos["bounce_qty"] == 0 else 0.0
        self.store.conn.execute(
            """UPDATE trench_desk_positions SET status='closed', closed_at=?, core_qty=0,
               bounce_qty=0, bounce_budget_usd=0, last_mark_price=?, last_mark_at=?
               WHERE desk_id=? AND mint=? AND opened_at=?""",
            (_now(), mark.price_usd, _now(), desk_id, pos["mint"], pos["opened_at"]))
        if unused:
            self.store.conn.execute(
                "UPDATE trench_desks SET cash_usd=cash_usd+? WHERE desk_id=?", (unused, desk_id))

    # -- cycle ---------------------------------------------------------------
    def manage_positions(self, desk_id: str, marks: dict[str, Mark], *,
                         now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        params = desk_params(self._desk(desk_id))
        for pos in self.store.open_positions(desk_id):
            mark = marks.get(pos["mint"])
            if mark is None:
                continue
            key = (desk_id, pos["mint"], pos["opened_at"])
            opened = datetime.fromisoformat(pos["opened_at"])
            if (now - opened).total_seconds() >= params.max_hold_seconds:
                self._close_position(desk_id, pos, mark, "max hold reached")
                continue
            if pos["core_qty"] > 0 and pos["core_entry_price"]:
                core_ret = mark.price_usd / pos["core_entry_price"] - 1
                if core_ret <= -params.core_stop_pct:
                    self._close_position(desk_id, pos, mark, "core stop loss")
                    continue
            if pos["bounce_qty"] > 0 and pos["bounce_entry_price"]:
                ret = mark.price_usd / pos["bounce_entry_price"] - 1
                reason = ("take profit" if ret >= params.take_profit_pct
                          else "stop loss" if ret <= -params.stop_loss_pct else None)
                if reason:
                    desk = self._desk(desk_id)
                    pnl = self._sell(desk, pos["mint"], pos["symbol"], "bounce",
                                     pos["bounce_qty"], pos["bounce_cost_usd"], mark, reason)
                    budget = max(0.0, pos["bounce_cost_usd"] + pnl)
                    self.store.conn.execute(
                        """UPDATE trench_desk_positions SET bounce_qty=0, bounce_cost_usd=0,
                           bounce_entry_price=NULL, bounce_budget_usd=?, last_exit_price=?
                           WHERE desk_id=? AND mint=? AND opened_at=?""",
                        (budget, mark.price_usd, *key))
                    # Reserve the bounce budget out of cash until re-entry or close.
                    self.store.conn.execute(
                        "UPDATE trench_desks SET cash_usd=cash_usd-? WHERE desk_id=?",
                        (budget, desk_id))
            elif (pos["bounce_qty"] == 0 and pos["last_exit_price"]
                  and pos["bounce_budget_usd"] > 1.0
                  and mark.price_usd <= pos["last_exit_price"] * (1 - params.rebuy_dip_pct)):
                desk = self._desk(desk_id)
                budget = pos["bounce_budget_usd"]
                self.store.conn.execute(
                    "UPDATE trench_desks SET cash_usd=cash_usd+? WHERE desk_id=?",
                    (budget, desk_id))
                qty, price = self._buy(desk, pos["mint"], pos["symbol"], "bounce", budget,
                                       mark, "rebuy after dip")
                self.store.conn.execute(
                    """UPDATE trench_desk_positions SET bounce_qty=?, bounce_cost_usd=?,
                       bounce_entry_price=?, bounce_budget_usd=0
                       WHERE desk_id=? AND mint=? AND opened_at=?""",
                    (qty, budget, price, *key))
            self.store.conn.execute(
                """UPDATE trench_desk_positions SET last_mark_price=?, last_mark_at=?
                   WHERE desk_id=? AND mint=? AND opened_at=? AND status='open'""",
                (mark.price_usd, now.isoformat(), *key))

    def try_enter(self, desk_id: str, mint: str, symbol: str, mark: Mark,
                  reason: str) -> bool:
        desk = self._desk(desk_id)
        params = desk_params(desk)
        open_rows = self.store.open_positions(desk_id)
        if len(open_rows) >= params.max_positions or any(r["mint"] == mint for r in open_rows):
            return False
        allocation = min(desk["cash_usd"], desk["starting_bankroll_usd"] * params.position_fraction)
        if allocation < 5.0:
            return False
        opened_at = _now()
        core_notional = allocation * params.core_fraction
        bounce_notional = allocation - core_notional
        core_qty = core_price = 0.0
        if core_notional >= 1.0:
            core_qty, core_price = self._buy(desk, mint, symbol, "core", core_notional, mark,
                                             reason)
        else:
            core_notional = 0.0
        desk = self._desk(desk_id)
        bounce_qty, bounce_price = self._buy(desk, mint, symbol, "bounce", bounce_notional,
                                             mark, reason)
        self.store.conn.execute(
            """INSERT INTO trench_desk_positions (desk_id, mint, symbol, opened_at, status,
               core_qty, core_cost_usd, core_entry_price, bounce_qty, bounce_cost_usd,
               bounce_entry_price, last_mark_price, last_mark_at)
               VALUES (?,?,?,?, 'open', ?,?,?,?,?,?,?,?)""",
            (desk_id, mint, symbol, opened_at, core_qty, core_notional, core_price or None,
             bounce_qty, bounce_notional, bounce_price, mark.price_usd, opened_at))
        self.store.event(desk_id, "paper_entry", mint=mint, symbol=symbol,
                         allocation_usd=allocation, reason=reason, quote=mark.price_usd,
                         quote_source=mark.source)
        return True

    def fire_if_needed(self, desk_id: str, marks: dict[str, Mark]) -> str | None:
        desk = self._desk(desk_id)
        if desk["status"] != "active" or desk["consecutive_losses"] < self.fire_after_losses:
            return None
        for pos in self.store.open_positions(desk_id):
            mark = marks.get(pos["mint"]) or Mark(
                pos["last_mark_price"] or pos["bounce_entry_price"] or pos["core_entry_price"],
                1e12, "last_mark")
            self._close_position(desk_id, pos, mark, "desk fired")
        desk = self._desk(desk_id)
        reason = f"{desk['consecutive_losses']} consecutive losses"
        self.store.conn.execute(
            """UPDATE trench_desks SET status='fired', fired_at=?, fired_reason=?
               WHERE desk_id=?""", (_now(), reason, desk_id))
        self.store.event(desk_id, "desk_fired", name=desk["name"], reason=reason,
                         equity_usd=desk["cash_usd"], realized_pnl_usd=desk["realized_pnl_usd"])
        generation = desk["generation"] + 1
        successor = self.store.create_desk(
            base_name=desk["base_name"], generation=generation,
            params=mutate_params(desk_params(desk), f"{desk_id}:{generation}"),
            bankroll_usd=max(desk["cash_usd"], 0.0), parent_desk_id=desk_id,
        )
        return successor

    def commit(self) -> None:
        self.store.conn.commit()


def desk_overview(db_path: str) -> dict[str, Any]:
    if not Path(db_path).exists():
        return {"desks": [], "positions": [], "recent_trades": [], "fired": [], "paper": True}
    store = TrenchDeskStore(db_path)
    try:
        desks = []
        for row in store.all_desks():
            positions = store.open_positions(row["desk_id"])
            mtm = sum(p["core_qty"] * (p["last_mark_price"] or 0)
                      + p["bounce_qty"] * (p["last_mark_price"] or 0)
                      + p["bounce_budget_usd"] for p in positions)
            equity = row["cash_usd"] + mtm
            desks.append({
                "desk_id": row["desk_id"], "name": row["name"], "status": row["status"],
                "generation": row["generation"], "params": json.loads(row["params_json"]),
                "starting_bankroll_usd": round(row["starting_bankroll_usd"], 2),
                "cash_usd": round(row["cash_usd"], 2), "equity_usd": round(equity, 2),
                "pnl_after_costs_usd": round(equity - row["starting_bankroll_usd"], 2),
                "realized_pnl_usd": round(row["realized_pnl_usd"], 2),
                "fees_usd": round(row["fees_usd"], 4), "slippage_usd": round(row["slippage_usd"], 4),
                "wins": row["wins"], "losses": row["losses"],
                "consecutive_losses": row["consecutive_losses"],
                "open_positions": len(positions), "fired_reason": row["fired_reason"],
                "parent_desk_id": row["parent_desk_id"],
            })
        positions = [dict(p) for p in store.open_positions()]
        trades = [dict(r) for r in store.conn.execute(
            "SELECT * FROM trench_desk_trades ORDER BY id DESC LIMIT 50")]
        events = [dict(r) for r in store.conn.execute(
            "SELECT * FROM trench_desk_events ORDER BY id DESC LIMIT 50")]
    finally:
        store.close()
    return {
        "paper": True,
        "live_execution": False,
        "desks": desks,
        "positions": positions,
        "recent_trades": trades,
        "recent_events": events,
        "fired": [d for d in desks if d["status"] == "fired"],
        "total_pnl_after_costs_usd": round(sum(
            d["pnl_after_costs_usd"] for d in desks if d["status"] == "active"
        ) + sum(d["pnl_after_costs_usd"] for d in desks if d["status"] == "fired"), 2),
    }
