from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DecisionQuality:
    entropy: float
    distance_from_market: float
    interval_width: float
    conviction_score: float


def binary_entropy(probability: float) -> float:
    if not 0 <= probability <= 1:
        raise ValueError("probability must be in [0, 1]")
    if probability in {0.0, 1.0}:
        return 0.0
    return -(
        probability * math.log2(probability)
        + (1 - probability) * math.log2(1 - probability)
    )


def assess_decision_quality(
    *,
    probability_yes: float,
    market_probability: float,
    lower_bound: float,
    upper_bound: float,
) -> DecisionQuality:
    if not 0 <= lower_bound <= probability_yes <= upper_bound <= 1:
        raise ValueError("invalid forecast interval")

    entropy = binary_entropy(probability_yes)
    distance = abs(probability_yes - market_probability)
    width = upper_bound - lower_bound

    # Conviction rises with market disagreement, but is discounted by uncertainty
    # and by forecasts near maximum entropy (p ~= 0.5).
    conviction = distance * (1 - min(width, 1.0)) * (1 - entropy * 0.5)

    return DecisionQuality(
        entropy=entropy,
        distance_from_market=distance,
        interval_width=width,
        conviction_score=max(0.0, conviction),
    )
