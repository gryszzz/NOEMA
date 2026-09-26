from __future__ import annotations

from .ledger import ForecastLedger
from .models import Action, Decision, Forecast, MarketSnapshot, Opportunity


def record_market_baseline(market: MarketSnapshot, ledger: ForecastLedger) -> None:
    """Record a transparent market benchmark, never an actionable signal."""
    if market.yes_ask is None:
        return
    probability = min(max(market.yes_ask, 0.0), 1.0)
    forecast = Forecast(
        market_id=market.market_id,
        venue=market.venue,
        probability_yes=probability,
        lower_bound=probability,
        upper_bound=probability,
        model_version="market-baseline-v1",
    )
    opportunity = Opportunity(
        forecast=forecast,
        snapshot=market,
        market_probability=probability,
        raw_edge=0.0,
        estimated_cost=0.0,
        uncertainty_penalty=0.0,
        robust_edge=0.0,
    )
    action = Action(
        decision=Decision.PASS,
        market_id=market.market_id,
        venue=market.venue,
        max_price=None,
        stake_usd=0.0,
        reason="Market baseline is a benchmark, not an independent edge estimate",
    )
    ledger.append(market, forecast, opportunity, action)
