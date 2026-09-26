from __future__ import annotations

from .ledger import ForecastLedger
from .market_probability import yes_midpoint
from .models import Action, Decision, Forecast, MarketSnapshot, Opportunity


def record_market_baseline(market: MarketSnapshot, ledger: ForecastLedger) -> None:
    """Record the ask for execution audit and the midpoint for forecast scoring."""
    if market.yes_ask is None:
        return
    probability = min(max(market.yes_ask, 0.0), 1.0)
    for version, price in (("market-baseline-v1", probability),
                           ("market-midpoint-v1", yes_midpoint(market))):
        if price is None:
            continue
        forecast = Forecast(
            market_id=market.market_id, venue=market.venue,
            probability_yes=price, lower_bound=price, upper_bound=price,
            model_version=version,
        )
        opportunity = Opportunity(
            forecast=forecast, snapshot=market,
            market_probability=market.yes_ask, raw_edge=0.0,
            estimated_cost=0.0, uncertainty_penalty=0.0, robust_edge=0.0,
        )
        action = Action(
            decision=Decision.PASS, market_id=market.market_id,
            venue=market.venue, max_price=None, stake_usd=0.0,
            reason="Market quote is a benchmark, not an independent edge estimate",
        )
        ledger.append(market, forecast, opportunity, action)
