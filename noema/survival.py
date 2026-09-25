from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class SurvivalPolicy:
    max_peak_drawdown_fraction: float = 0.08
    max_daily_loss_fraction: float = 0.03
    max_open_exposure_fraction: float = 0.10
    max_market_exposure_fraction: float = 0.02
    min_cash_reserve_fraction: float = 0.50
    max_consecutive_losses: int = 5
    max_account_data_age_seconds: float = 15.0


@dataclass(frozen=True)
class SurvivalState:
    starting_equity: float
    peak_equity: float
    current_equity: float
    daily_start_equity: float
    open_exposure: float
    market_exposure: float
    cash_balance: float
    consecutive_losses: int
    account_data_as_of: datetime | None


@dataclass(frozen=True)
class SurvivalDecision:
    trading_allowed: bool
    risk_multiplier: float
    reasons: tuple[str, ...]


class CapitalGuardian:
    def __init__(self, policy: SurvivalPolicy | None = None) -> None:
        self.policy = policy or SurvivalPolicy()

    def assess(
        self,
        state: SurvivalState,
        *,
        now: datetime | None = None,
    ) -> SurvivalDecision:
        now = now or datetime.now(UTC)
        reasons: list[str] = []

        if state.current_equity <= 0:
            return SurvivalDecision(False, 0.0, ("equity depleted",))

        peak_drawdown = max(
            0.0,
            (state.peak_equity - state.current_equity) / max(state.peak_equity, 1e-9),
        )
        daily_loss = max(
            0.0,
            (state.daily_start_equity - state.current_equity)
            / max(state.daily_start_equity, 1e-9),
        )
        open_exposure_fraction = state.open_exposure / state.current_equity
        market_exposure_fraction = state.market_exposure / state.current_equity
        cash_fraction = state.cash_balance / state.current_equity

        if state.account_data_as_of is None:
            reasons.append("account state timestamp unavailable")
        else:
            age = (now - state.account_data_as_of).total_seconds()
            if age < 0:
                reasons.append("account state timestamp is in the future")
            elif age > self.policy.max_account_data_age_seconds:
                reasons.append(f"account state stale by {age:.1f}s")

        if peak_drawdown >= self.policy.max_peak_drawdown_fraction:
            reasons.append("peak drawdown kill switch")
        if daily_loss >= self.policy.max_daily_loss_fraction:
            reasons.append("daily loss kill switch")
        if open_exposure_fraction >= self.policy.max_open_exposure_fraction:
            reasons.append("portfolio exposure cap")
        if market_exposure_fraction >= self.policy.max_market_exposure_fraction:
            reasons.append("single-market exposure cap")
        if cash_fraction < self.policy.min_cash_reserve_fraction:
            reasons.append("cash reserve below floor")
        if state.consecutive_losses >= self.policy.max_consecutive_losses:
            reasons.append("consecutive-loss quarantine")

        if reasons:
            return SurvivalDecision(False, 0.0, tuple(reasons))

        risk_multiplier = 1.0
        if peak_drawdown >= self.policy.max_peak_drawdown_fraction / 2:
            risk_multiplier = min(risk_multiplier, 0.5)
        if daily_loss >= self.policy.max_daily_loss_fraction / 2:
            risk_multiplier = min(risk_multiplier, 0.5)
        if state.consecutive_losses >= max(1, self.policy.max_consecutive_losses // 2):
            risk_multiplier = min(risk_multiplier, 0.5)

        return SurvivalDecision(True, risk_multiplier, ())
