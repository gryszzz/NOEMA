from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DriftResult:
    drifted: bool
    cumulative_shift: float
    observations: int
    direction: str


def detect_brier_drift(
    historical_brier: float,
    recent_scores: list[float],
    *,
    allowance: float = 0.005,
    threshold: float = 0.05,
) -> DriftResult:
    if historical_brier < 0:
        raise ValueError("historical_brier must be non-negative")
    if allowance < 0 or threshold <= 0:
        raise ValueError("invalid drift parameters")

    positive = 0.0
    negative = 0.0
    for score in recent_scores:
        if score < 0:
            raise ValueError("Brier scores must be non-negative")
        delta = score - historical_brier
        positive = max(0.0, positive + delta - allowance)
        negative = min(0.0, negative + delta + allowance)

    if positive >= threshold:
        return DriftResult(True, positive, len(recent_scores), "worsening")
    if abs(negative) >= threshold:
        return DriftResult(True, negative, len(recent_scores), "improving")
    return DriftResult(False, positive if positive else negative, len(recent_scores), "stable")
