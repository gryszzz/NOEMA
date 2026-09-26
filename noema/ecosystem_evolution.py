from __future__ import annotations

from dataclasses import dataclass

from .ecosystem_controller import ensure_default_specialists
from .evolution_controller import SpecialistReviewResult, review_specialist
from .specialist_evidence_sources import kalshi_history_evidence, trench1_evidence


@dataclass(frozen=True)
class EcosystemEvolutionReview:
    kalshi: SpecialistReviewResult
    trench: SpecialistReviewResult


def evolve_default_specialists(
    db_path: str,
    *,
    trench_horizon_seconds: int = 3600,
) -> EcosystemEvolutionReview:
    """Review the default specialist set from newly resolved forward evidence.

    Reviews are idempotent with respect to unchanged evidence: the underlying controller
    will not advance promotion/demotion streaks when no new evidence arrived.
    """

    ensure_default_specialists(db_path)
    kalshi = review_specialist(
        db_path,
        specialist="kalshi-history",
        evidence=kalshi_history_evidence(db_path),
    )
    trench = review_specialist(
        db_path,
        specialist="trench-1",
        evidence=trench1_evidence(
            db_path,
            horizon_seconds=trench_horizon_seconds,
        ),
    )
    return EcosystemEvolutionReview(kalshi=kalshi, trench=trench)
