from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

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
        if not _finite_number(risk_multiplier) or not 0 <= risk_multiplier <= 1:
            return self._pass(opportunity, "risk multiplier is malformed or outside [0, 1]")
        if risk_multiplier == 0:
            return self._pass(opportunity, "external risk gate set multiplier to zero")

        if not self._valid_policy():
            return self._pass(opportunity, "deterministic risk policy is malformed")
        if not _finite_number(bankroll_usd) or bankroll_usd < 0:
            return self._pass(opportunity, "bankroll is malformed or negative")

        s = opportunity.snapshot
        f = opportunity.forecast

        if not self._valid_opportunity(opportunity):
            return self._pass(opportunity, "opportunity contains malformed or contradictory inputs")

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

    def _valid_policy(self) -> bool:
        p = self.policy
        values = (
            p.min_robust_edge,
            p.min_liquidity_usd,
            p.max_spread,
            p.max_stake_usd,
            p.max_fraction_of_bankroll,
            p.max_forecast_width,
            p.max_market_data_age_seconds,
        )
        if not all(_finite_number(value) for value in values):
            return False
        return (
            0 <= p.min_robust_edge <= 1
            and p.min_liquidity_usd >= 0
            and 0 <= p.max_spread <= 1
            and p.max_stake_usd >= 0
            and 0 <= p.max_fraction_of_bankroll <= 1
            and 0 <= p.max_forecast_width <= 1
            and p.max_market_data_age_seconds >= 0
        )

    @staticmethod
    def _valid_opportunity(opportunity: Opportunity) -> bool:
        snapshot = opportunity.snapshot
        forecast = opportunity.forecast
        captured_at = snapshot.captured_at
        if (not isinstance(captured_at, datetime) or captured_at.tzinfo is None
                or captured_at.utcoffset() is None):
            return False
        numeric_fields = (
            opportunity.market_probability,
            opportunity.raw_edge,
            opportunity.estimated_cost,
            opportunity.uncertainty_penalty,
            opportunity.robust_edge,
            forecast.probability_yes,
            forecast.lower_bound,
            forecast.upper_bound,
            snapshot.yes_bid,
            snapshot.yes_ask,
            snapshot.liquidity_usd,
        )
        if not all(_finite_number(value) for value in numeric_fields):
            return False
        if (forecast.market_id != snapshot.market_id or forecast.venue != snapshot.venue
                or forecast.model_version == ""):
            return False
        if not all(0 <= value <= 1 for value in (
            opportunity.market_probability,
            forecast.probability_yes,
            forecast.lower_bound,
            forecast.upper_bound,
            snapshot.yes_bid,
            snapshot.yes_ask,
        )):
            return False
        return (
            forecast.lower_bound <= forecast.probability_yes <= forecast.upper_bound
            and snapshot.yes_bid <= snapshot.yes_ask
            and snapshot.liquidity_usd >= 0
            and opportunity.estimated_cost >= 0
            and opportunity.uncertainty_penalty >= 0
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


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
    )
