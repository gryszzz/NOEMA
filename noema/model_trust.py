from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPerformance:
    name: str
    resolved: int
    mean_log_loss: float
    market_mean_log_loss: float
    mean_brier: float
    market_mean_brier: float


@dataclass(frozen=True)
class ModelTrust:
    name: str
    reliability: float
    evidence_strength: float
    reason: str


def derive_model_trust(
    performance: ModelPerformance,
    *,
    min_resolved: int = 30,
    full_confidence_resolved: int = 500,
) -> ModelTrust:
    if performance.resolved <= 0:
        return ModelTrust(performance.name, 0.0, 0.0, "no resolved evidence")

    evidence_strength = min(
        1.0,
        performance.resolved / max(full_confidence_resolved, 1),
    )

    if performance.resolved < min_resolved:
        return ModelTrust(
            performance.name,
            0.1 * evidence_strength,
            evidence_strength,
            "insufficient resolved sample",
        )

    log_advantage = performance.market_mean_log_loss - performance.mean_log_loss
    brier_advantage = performance.market_mean_brier - performance.mean_brier

    # Smooth bounded score: negative or weak models stay low-trust.
    combined = 3.0 * log_advantage + 2.0 * brier_advantage
    quality = 1 / (1 + math.exp(-combined * 8))
    reliability = max(0.0, min(1.5, quality * evidence_strength * 1.5))

    reason = (
        "outperforming market baseline"
        if log_advantage > 0 and brier_advantage > 0
        else "mixed or negative baseline performance"
    )
    return ModelTrust(
        performance.name,
        reliability,
        evidence_strength,
        reason,
    )
