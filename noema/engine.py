from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .ledger import ForecastLedger
from .models import Forecast, MarketSnapshot, Opportunity
from .risk import RiskEngine
from .venues.base import VenueAdapter


class Forecaster(Protocol):
    async def forecast(self, market: MarketSnapshot) -> Forecast | None: ...


@dataclass
class CostModel:
    slippage: float = 0.01
    fees: float = 0.005

    def estimate(self, market: MarketSnapshot) -> float:
        spread = 0.0
        if market.yes_bid is not None and market.yes_ask is not None:
            spread = max(0.0, market.yes_ask - market.yes_bid)
        return self.slippage + self.fees + spread / 2


class NoemaEngine:
    def __init__(
        self,
        venue: VenueAdapter,
        forecaster: Forecaster,
        risk: RiskEngine,
        ledger: ForecastLedger,
        cost_model: CostModel | None = None,
    ) -> None:
        self.venue = venue
        self.forecaster = forecaster
        self.risk = risk
        self.ledger = ledger
        self.cost_model = cost_model or CostModel()

    async def scan_once(self, bankroll_usd: float) -> list[str]:
        results: list[str] = []

        async for market in self.venue.markets():
            forecast = await self.forecaster.forecast(market)
            if forecast is None or market.yes_ask is None:
                continue

            market_probability = market.yes_ask
            raw_edge = forecast.probability_yes - market_probability
            uncertainty_penalty = max(
                0.0,
                (forecast.upper_bound - forecast.lower_bound) / 2,
            )
            estimated_cost = self.cost_model.estimate(market)
            robust_edge = raw_edge - uncertainty_penalty - estimated_cost

            opportunity = Opportunity(
                forecast=forecast,
                snapshot=market,
                market_probability=market_probability,
                raw_edge=raw_edge,
                estimated_cost=estimated_cost,
                uncertainty_penalty=uncertainty_penalty,
                robust_edge=robust_edge,
            )
            action = self.risk.decide(opportunity, bankroll_usd)
            self.ledger.append(market, forecast, opportunity, action)

            if action.decision.value.startswith("live_"):
                if not self.venue.supports_live_execution:
                    raise RuntimeError("risk engine requested live action on non-live venue")
                await self.venue.execute(action)

            results.append(f"{market.venue}:{market.market_id}:{action.decision.value}")

        return results
