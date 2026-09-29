from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .execution_gateway import ExecutionGateway, ExecutionProposal
from .ledger import ForecastLedger
from .models import Forecast, MarketSnapshot, Opportunity
from .risk import RiskEngine
from .validation import validate_market_snapshot
from .venues.base import VenueAdapter


class Forecaster(Protocol):
    async def forecast(self, market: MarketSnapshot) -> Forecast | None: ...


@dataclass
class CostModel:
    slippage: float = 0.01
    fees: float = 0.03  # Paper fallback only; not a verified market-specific quote.

    def estimate(self, market: MarketSnapshot) -> float:
        # The raw YES edge already subtracts the ask, so spread is paid there.
        return self.slippage + self.fees


class NoemaEngine:
    def __init__(
        self,
        venue: VenueAdapter,
        forecaster: Forecaster,
        risk: RiskEngine,
        ledger: ForecastLedger,
        cost_model: CostModel | None = None,
        execution_gateway: ExecutionGateway | None = None,
    ) -> None:
        self.venue = venue
        self.forecaster = forecaster
        self.risk = risk
        self.ledger = ledger
        self.cost_model = cost_model or CostModel()
        self.execution_gateway = execution_gateway or ExecutionGateway(
            os.getenv("NOEMA_DB_PATH", "data/noema.db")
        )

    async def scan_once(
        self, bankroll_usd: float, *, mission_id: str | None = None,
    ) -> list[str]:
        results: list[str] = []

        async for market in self.venue.markets():
            validation = validate_market_snapshot(market)
            if not validation.valid:
                results.append(
                    f"{market.venue}:{market.market_id}:invalid:"
                    + "|".join(validation.issues)
                )
                continue

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
                if not mission_id or not forecast.evidence_ids:
                    results.append(
                        f"{market.venue}:{market.market_id}:gateway_rejected:"
                        "mission and evidence are required"
                    )
                    continue
                proposal = ExecutionProposal.from_opportunity(
                    proposal_id=f"{mission_id}:{market.venue}:{market.market_id}:{forecast.created_at.isoformat()}",
                    mission_id=mission_id,
                    opportunity=opportunity,
                    action=action,
                    evidence_refs=forecast.evidence_ids,
                    expires_at=datetime.now(UTC) + timedelta(
                        seconds=max(1, int(self.risk.policy.max_market_data_age_seconds)),
                    ),
                )
                self.execution_gateway.record_proposal(proposal)
                gateway_result = await self.execution_gateway.submit_prediction_order(
                    proposal=proposal,
                    opportunity=opportunity,
                    action=action,
                    risk_engine=self.risk,
                    venue_adapter=self.venue,
                    bankroll_usd=bankroll_usd,
                )
                if not gateway_result.allowed:
                    results.append(
                        f"{market.venue}:{market.market_id}:gateway_rejected:"
                        + "|".join(gateway_result.reasons)
                    )
                    continue

            results.append(f"{market.venue}:{market.market_id}:{action.decision.value}")

        return results
