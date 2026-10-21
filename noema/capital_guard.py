"""Capital guard: a rising bankroll floor, a daily loss stop, and edge-scaled sizing.

Fail closed. Live trading on every path (Kalshi, Polymarket US, Solana, EVM) is
refused unless a fresh equity snapshot exists and the guard is not tripped.

* Floor: ``NOEMA_CAPITAL_FLOOR_FRACTION`` (default 0.75) of peak equity. Peak
  only rises (profit ratchet), so the floor never decreases. Deposits raise the
  peak and the floor with them.
* Daily stop: if equity falls ``NOEMA_DAILY_LOSS_STOP_FRACTION`` (default 0.10)
  below the day's opening equity, live trading halts.
* Either breach latches a trip. Only an explicit owner reset clears it.
* Per-trade size scales with proven edge: 3% of equity at the first live tier,
  5% and 8% only when the autonomy ladder *and* the agent's measured accuracy
  both earn it. Within a tier, fractional Kelly sizes the pick, so weak or
  uncalibrated picks get less. No trade may risk more than the headroom above
  the floor or the remaining daily loss budget.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

TIER_MAX_FRACTION: dict[str, Decimal] = {
    "micro": Decimal("0.03"),
    "proven": Decimal("0.03"),
    "self_funded": Decimal("0.05"),
    "expansion": Decimal("0.08"),
}
_TIER_ORDER = ("micro", "proven", "self_funded", "expansion")


def _env_fraction(name: str, default: str, low: str, high: str) -> Decimal:
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        value = Decimal(default)
    if not value.is_finite():
        value = Decimal(default)
    # Clamp so a typo can never loosen protection beyond sane bounds.
    return min(max(value, Decimal(low)), Decimal(high))


def floor_fraction() -> Decimal:
    return _env_fraction("NOEMA_CAPITAL_FLOOR_FRACTION", "0.75", "0.50", "0.95")


def daily_stop_fraction() -> Decimal:
    return _env_fraction("NOEMA_DAILY_LOSS_STOP_FRACTION", "0.10", "0.01", "0.10")


def max_equity_age() -> timedelta:
    try:
        seconds = int(os.getenv("NOEMA_EQUITY_MAX_AGE_SECONDS", "900"))
    except ValueError:
        seconds = 900
    return timedelta(seconds=min(max(seconds, 30), 3600))


@dataclass(frozen=True)
class GuardState:
    peak_equity: Decimal
    floor: Decimal
    equity: Decimal
    day: str
    day_start_equity: Decimal
    tripped: bool
    trip_reason: str | None
    updated_at: datetime


class CapitalGuard:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.getenv("NOEMA_DB_PATH", "data/noema.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS capital_guard (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                peak_equity TEXT NOT NULL, floor TEXT NOT NULL, equity TEXT NOT NULL,
                day TEXT NOT NULL, day_start_equity TEXT NOT NULL,
                tripped INTEGER NOT NULL, trip_reason TEXT, updated_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS capital_guard_events (
                at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)""")

    # -- state -------------------------------------------------------------
    def state(self) -> GuardState | None:
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            row = conn.execute("SELECT peak_equity, floor, equity, day, day_start_equity, "
                               "tripped, trip_reason, updated_at FROM capital_guard WHERE id=1").fetchone()
        if row is None:
            return None
        return GuardState(Decimal(row[0]), Decimal(row[1]), Decimal(row[2]), row[3],
                          Decimal(row[4]), bool(row[5]), row[6], datetime.fromisoformat(row[7]))

    def record_equity(self, equity_usd: Decimal | float | str, *, deposit_usd: Decimal | float | str = 0,
                      now: datetime | None = None) -> GuardState:
        """Record total equity across all venues and wallets. Deposits raise the baseline."""
        now = now or datetime.now(UTC)
        equity = Decimal(str(equity_usd))
        deposit = Decimal(str(deposit_usd))
        if not equity.is_finite() or equity < 0 or not deposit.is_finite() or deposit < 0:
            raise ValueError("equity and deposit must be finite and non-negative")
        day = now.astimezone(UTC).date().isoformat()
        frac = floor_fraction()
        current = self.state()
        if current is None:
            peak, floor_value, day_start, tripped, reason = equity, equity * frac, equity, False, None
        else:
            peak = max(current.peak_equity + deposit, equity)
            floor_value = max(current.floor, peak * frac)  # ratchet: never decreases
            day_start = current.day_start_equity + deposit if current.day == day else equity
            tripped, reason = current.tripped, current.trip_reason
        if not tripped and equity > 0:
            if equity < floor_value:
                tripped, reason = True, f"equity fell below the capital floor ({frac:.0%} of peak)"
            elif day_start > 0 and (day_start - equity) / day_start >= daily_stop_fraction():
                tripped, reason = True, f"daily loss stop hit ({daily_stop_fraction():.0%})"
        if equity <= 0 and peak > 0:
            tripped, reason = True, "equity depleted"
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("""INSERT INTO capital_guard VALUES (1,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET peak_equity=excluded.peak_equity, floor=excluded.floor,
                equity=excluded.equity, day=excluded.day, day_start_equity=excluded.day_start_equity,
                tripped=excluded.tripped, trip_reason=excluded.trip_reason, updated_at=excluded.updated_at""",
                         (str(peak), str(floor_value), str(equity), day, str(day_start),
                          int(tripped), reason, now.isoformat()))
            if tripped and (current is None or not current.tripped):
                conn.execute("INSERT INTO capital_guard_events VALUES (?,?,?)",
                             (now.isoformat(), "trip", reason or ""))
        state = self.state()
        assert state is not None
        return state

    def owner_reset(self, *, confirm: str, now: datetime | None = None) -> GuardState:
        """Owner-only: clear a trip. Floor and peak are kept; the day restarts at current equity."""
        if confirm != "I ACCEPT THE LOSS":
            raise PermissionError("owner reset requires the explicit confirmation phrase")
        now = now or datetime.now(UTC)
        current = self.state()
        if current is None:
            raise RuntimeError("no capital guard state to reset")
        floor_value = current.floor if current.equity >= current.floor else current.equity * floor_fraction()
        peak = current.peak_equity if current.equity >= current.floor else current.equity
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("UPDATE capital_guard SET tripped=0, trip_reason=NULL, floor=?, peak_equity=?, "
                         "day_start_equity=equity, updated_at=? WHERE id=1",
                         (str(floor_value), str(peak), now.isoformat()))
            conn.execute("INSERT INTO capital_guard_events VALUES (?,?,?)",
                         (now.isoformat(), "owner_reset", current.trip_reason or ""))
        state = self.state()
        assert state is not None
        return state

    # -- gates -------------------------------------------------------------
    def block_reasons(self, notional_usd: Any = None, *, tier: str | None = None,
                      now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        state = self.state()
        if state is None:
            return ["capital guard has no equity baseline"]
        reasons: list[str] = []
        if state.tripped:
            reasons.append(f"capital guard tripped: {state.trip_reason}; owner reset required")
        if now - state.updated_at > max_equity_age():
            reasons.append("capital guard equity snapshot is stale")
        if state.equity <= state.floor:
            reasons.append("equity is at or below the capital floor")
        if notional_usd is not None:
            try:
                notional = Decimal(str(notional_usd))
            except (InvalidOperation, ValueError):
                notional = None
            if notional is None or not notional.is_finite() or notional <= 0:
                reasons.append("capital guard cannot size a malformed notional")
            elif notional > self.max_trade_usd(tier=tier, state=state, now=now):
                reasons.append("trade exceeds the capital guard per-trade limit")
        return reasons

    def max_trade_usd(self, *, tier: str | None, state: GuardState | None = None,
                      now: datetime | None = None) -> Decimal:
        state = state or self.state()
        if state is None or state.tripped:
            return Decimal(0)
        tier_key = (tier or os.getenv("NOEMA_AUTONOMY_LEVEL", "")).strip().lower()
        fraction = TIER_MAX_FRACTION.get(tier_key)
        if fraction is None:
            return Decimal(0)
        today = (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()
        day_start = state.day_start_equity if state.day == today else state.equity
        daily_budget = day_start * daily_stop_fraction() - max(Decimal(0), day_start - state.equity)
        headroom = state.equity - state.floor
        return max(Decimal(0), min(state.equity * fraction, headroom, daily_budget))


# -- edge-scaled sizing ------------------------------------------------------

@dataclass(frozen=True)
class AgentSkill:
    resolved: int
    brier: float | None
    market_brier: float | None
    ev_after_cost: float | None


def proven_tier(skill: AgentSkill | None) -> str:
    """The highest tier an agent's own measured accuracy supports."""
    if skill is None or skill.brier is None or skill.ev_after_cost is None or skill.ev_after_cost <= 0:
        return "micro"
    beats_market = skill.market_brier is not None and skill.brier < skill.market_brier
    if skill.resolved >= 200 and beats_market and skill.brier <= 0.18:
        return "expansion"
    if skill.resolved >= 100 and beats_market and skill.brier <= 0.20:
        return "self_funded"
    if skill.resolved >= 30 and skill.brier <= 0.22:
        return "proven"
    return "micro"


def effective_tier(ladder_tier: str, skill: AgentSkill | None) -> str:
    ladder = ladder_tier.strip().lower()
    if ladder not in _TIER_ORDER:
        return ""
    return _TIER_ORDER[min(_TIER_ORDER.index(ladder), _TIER_ORDER.index(proven_tier(skill)))]


def size_prediction_stake(*, equity_cap_usd: Decimal, equity_usd: Decimal, tier: str,
                          probability: float, price: float, skill: AgentSkill | None,
                          kelly_fraction: float = 0.25) -> Decimal:
    """Fractional Kelly for a YES contract at ``price``, capped by the tier and the guard.

    ``equity_cap_usd`` is ``CapitalGuard.max_trade_usd`` for the tier (floor and
    daily headroom already applied). Uncalibrated agents get half the Kelly fraction.
    """
    if not (0 < price < 1) or not (0 <= probability <= 1) or equity_usd <= 0:
        return Decimal(0)
    edge = probability - price
    if edge <= 0:
        return Decimal(0)
    kelly = edge / (1 - price)
    calibrated = skill is not None and skill.resolved >= 30 and skill.brier is not None and skill.brier <= 0.22
    frac = kelly_fraction * (1.0 if calibrated else 0.5)
    tier_cap = equity_usd * TIER_MAX_FRACTION.get(tier, Decimal(0))
    stake = equity_usd * Decimal(str(round(kelly * frac, 6)))
    return max(Decimal(0), min(stake, tier_cap, equity_cap_usd)).quantize(Decimal("0.01"))
