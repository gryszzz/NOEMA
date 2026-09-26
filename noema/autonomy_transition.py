from __future__ import annotations

from dataclasses import dataclass

from .economic_models import AutonomyLevel

_LEVELS = (
    AutonomyLevel.SHADOW,
    AutonomyLevel.PAPER,
    AutonomyLevel.DEMO,
    AutonomyLevel.MICRO,
    AutonomyLevel.PROVEN,
    AutonomyLevel.SELF_FUNDED,
    AutonomyLevel.EXPANSION,
)


@dataclass(frozen=True)
class AutonomyTransition:
    previous: AutonomyLevel
    evidence_level: AutonomyLevel
    next_level: AutonomyLevel
    promoted: bool
    demoted: bool
    reason: str


def transition_autonomy(
    current: AutonomyLevel,
    evidence_level: AutonomyLevel,
) -> AutonomyTransition:
    current_index = _LEVELS.index(current)
    evidence_index = _LEVELS.index(evidence_level)

    if evidence_index < current_index:
        return AutonomyTransition(
            previous=current,
            evidence_level=evidence_level,
            next_level=evidence_level,
            promoted=False,
            demoted=True,
            reason="evidence deteriorated; downshift immediately",
        )

    if evidence_index > current_index:
        next_level = _LEVELS[min(current_index + 1, len(_LEVELS) - 1)]
        return AutonomyTransition(
            previous=current,
            evidence_level=evidence_level,
            next_level=next_level,
            promoted=True,
            demoted=False,
            reason="promotion limited to one autonomy level per review",
        )

    return AutonomyTransition(
        previous=current,
        evidence_level=evidence_level,
        next_level=current,
        promoted=False,
        demoted=False,
        reason="maintain current autonomy level",
    )
