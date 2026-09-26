from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StrategyStatus(str, Enum):
    ACTIVE = "active"
    THROTTLED = "throttled"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class StrategyEvidence:
    resolved_forecasts: int
    brier: float
    market_baseline_brier: float
    after_cost_return: float
    max_drawdown_fraction: float
    calibration_error: float | None = None
    research_credible: bool | None = None


@dataclass(frozen=True)
class StrategyHealth:
    status: StrategyStatus
    risk_multiplier: float
    reason: str


def evaluate_strategy(
    evidence: StrategyEvidence,
    *,
    min_resolved: int = 100,
    max_drawdown_fraction: float = 0.10,
) -> StrategyHealth:
    if evidence.resolved_forecasts < min_resolved:
        return StrategyHealth(
            StrategyStatus.QUARANTINED,
            0.0,
            "insufficient out-of-sample resolved forecasts",
        )
    if evidence.max_drawdown_fraction >= max_drawdown_fraction:
        return StrategyHealth(StrategyStatus.QUARANTINED, 0.0, "drawdown limit exceeded")
    if evidence.research_credible is False:
        return StrategyHealth(
            StrategyStatus.QUARANTINED,
            0.0,
            "research credibility gate failed",
        )
    if evidence.calibration_error is not None and evidence.calibration_error > 0.10:
        return StrategyHealth(
            StrategyStatus.QUARANTINED,
            0.0,
            "calibration error above threshold",
        )
    if evidence.after_cost_return <= 0:
        return StrategyHealth(
            StrategyStatus.QUARANTINED,
            0.0,
            "non-positive after-cost performance",
        )
    if evidence.brier >= evidence.market_baseline_brier:
        return StrategyHealth(
            StrategyStatus.QUARANTINED,
            0.0,
            "does not beat market-probability baseline on Brier score",
        )

    relative_improvement = (
        evidence.market_baseline_brier - evidence.brier
    ) / max(evidence.market_baseline_brier, 1e-12)
    if relative_improvement < 0.05:
        return StrategyHealth(
            StrategyStatus.THROTTLED,
            0.25,
            "edge exists but calibration advantage is narrow",
        )
    return StrategyHealth(StrategyStatus.ACTIVE, 1.0, "promotion gates passed")
