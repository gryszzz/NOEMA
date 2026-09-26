from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SpecialistState(str, Enum):
    SHADOW = "shadow"
    PAPER = "paper"
    ACTIVE_RESEARCH = "active_research"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class SpecialistProfile:
    name: str
    family: str
    state: SpecialistState
    resolved: int
    reliability: float
    calibration_error: float | None
    after_cost_return: float | None
    drawdown_fraction: float | None


def specialist_attention_multiplier(profile: SpecialistProfile) -> float:
    if profile.state is SpecialistState.QUARANTINED:
        return 0.0
    if profile.state is SpecialistState.SHADOW:
        return 0.10
    if profile.state is SpecialistState.PAPER:
        return 0.35

    reliability = max(0.0, min(profile.reliability, 1.5))
    sample_factor = min(1.0, profile.resolved / 500.0)

    calibration_factor = 1.0
    if profile.calibration_error is not None:
        calibration_factor = max(0.0, 1 - min(profile.calibration_error, 0.25) / 0.25)

    drawdown_factor = 1.0
    if profile.drawdown_fraction is not None:
        drawdown_factor = max(0.0, 1 - min(profile.drawdown_fraction, 0.20) / 0.20)

    return min(
        1.0,
        reliability / 1.5 * sample_factor * calibration_factor * drawdown_factor,
    )
