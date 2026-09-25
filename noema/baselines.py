from __future__ import annotations

from noema.models import Forecast, MarketSnapshot


class MarketBaselineForecaster:
    """Uses current market ask as the naive benchmark probability.

    This is not an alpha model. It exists so every learned model must beat a
    transparent baseline out-of-sample rather than being judged in isolation.
    """

    model_version = "market-baseline-v1"

    async def forecast(self, market: MarketSnapshot) -> Forecast | None:
        if market.yes_ask is None:
            return None

        probability = min(max(market.yes_ask, 0.0), 1.0)
        return Forecast(
            market_id=market.market_id,
            venue=market.venue,
            probability_yes=probability,
            lower_bound=probability,
            upper_bound=probability,
            model_version=self.model_version,
        )
