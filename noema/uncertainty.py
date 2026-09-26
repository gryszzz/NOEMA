from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class UncertaintyInputs:
    interval_width: float
    model_disagreement: float
    data_staleness_seconds: float
    liquidity_usd: float | None
    spread: float | None


@dataclass(frozen=True)
class UncertaintyAssessment:
    penalty: float
    components: dict[str, float]


def assess_uncertainty(inputs: UncertaintyInputs) -> UncertaintyAssessment:
    interval = max(0.0, inputs.interval_width) / 2
    disagreement = max(0.0, inputs.model_disagreement) * 0.75
    staleness = min(max(inputs.data_staleness_seconds, 0.0) / 120.0, 1.0) * 0.03

    if inputs.liquidity_usd is None or inputs.liquidity_usd <= 0:
        liquidity = 0.05
    else:
        liquidity = 0.04 / math.sqrt(max(inputs.liquidity_usd / 1000.0, 1.0))

    spread = max(inputs.spread or 0.0, 0.0) / 2
    components = {
        "forecast_interval": interval,
        "model_disagreement": disagreement,
        "data_staleness": staleness,
        "liquidity": liquidity,
        "spread": spread,
    }
    return UncertaintyAssessment(
        penalty=sum(components.values()),
        components=components,
    )
