from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import Action, Decision, Mode, Opportunity


@dataclass(frozen=True)
class RiskPolicy:
    mode: Mode = Mode.PAPER
    min_robust_edge: float = 0.03
    min_liquidity_usd: float = 1_000.0
    max_spread: float = 0.08
    max_stake_usd: float = 10.0
    max_fraction_of_bankroll: float = 0.005
    max_forecast_width: float = 0.20
    max_market_data_age_seconds: float = 10.0


class RiskEngine:
    def __init__(self, policy: RiskPolicy) -> None:
        self.policy = policy

    def decide(
        self,
        opportunity: Opportunity,
        bankroll_usd: float,
        *,
        risk_multiplier: float = 1.0,
    ) -> Action:
        if not 0 <= risk_multiplier <= 1:
            raise ValueError("risk_multiplier must be in [0, 1]")
        if risk_multiplier == 0:
            return self._pass(opportunity, "external risk gate set multiplier to zero")

        s = opportunity.snapshot
        f = opportunity.forecast

        quote_age = (datetime.now(UTC) - s.captured_at).total_seconds()
        if quote_age < 0:
            return self._pass(opportunity, "market snapshot timestamp is in the future")
        if quote_age > self.policy.max_market_data_age_seconds:
            return self._pass(opportunity, f"market snapshot stale by {quote_age:.1f}s")

        if s.yes_ask is None or s.yes_bid is None:
            return self._pass(opportunity, "missing tradable YES quote")

        spread = s.yes_ask - s.yes_bid
        if spread < 0 or spread > self.policy.max_spread:
            return self._pass(opportunity, f"spread {spread:.3f} outside policy")

        if s.liquidity_usd is None or s.liquidity_usd < self.policy.min_liquidity_usd:
            return self._pass(opportunity, "insufficient or unknown liquidity")

        if f.upper_bound - f.lower_bound > self.policy.max_forecast_width:
            return self._pass(opportunity, "forecast uncertainty too wide")

        if opportunity.robust_edge < self.policy.min_robust_edge:
            return self._pass(opportunity, "robust edge below threshold")

        stake = min(
            self.policy.max_stake_usd * risk_multiplier,
            max(0.0, bankroll_usd * self.policy.max_fraction_of_bankroll * risk_multiplier),
        )
        if stake <= 0:
            return self._pass(opportunity, "zero risk budget")

        live = self.policy.mode is Mode.LIVE
        decision = Decision.LIVE_BUY_YES if live else Decision.PAPER_BUY_YES
        return Action(
            decision=decision,
            market_id=s.market_id,
            venue=s.venue,
            max_price=s.yes_ask,
            stake_usd=round(stake, 2),
            reason=f"robust edge {opportunity.robust_edge:.3f} passed deterministic gates",
        )

    @staticmethod
    def _pass(opportunity: Opportunity, reason: str) -> Action:
        return Action(
            decision=Decision.PASS,
            market_id=opportunity.snapshot.market_id,
            venue=opportunity.snapshot.venue,
            max_price=None,
            stake_usd=0.0,
            reason=reason,
        )
