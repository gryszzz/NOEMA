from __future__ import annotations

from dataclasses import dataclass

from .ecosystem_controller import ensure_default_specialists
from .evolution_controller import SpecialistReviewResult, review_specialist
from .experiment_factory import RegisteredExperiment, register_challengers
from .specialist_evidence_sources import kalshi_history_evidence, trench1_evidence


@dataclass(frozen=True)
class SpecialistEvolutionCycle:
    review: SpecialistReviewResult
    experiments: tuple[RegisteredExperiment, ...]


@dataclass(frozen=True)
class EcosystemEvolutionReview:
    kalshi: SpecialistEvolutionCycle
    trench: SpecialistEvolutionCycle


def _review_with_challengers(
    db_path: str,
    *,
    specialist: str,
    evidence,
) -> SpecialistEvolutionCycle:
    review = review_specialist(
        db_path,
        specialist=specialist,
        evidence=evidence,
    )
    experiments = (
        register_challengers(
            db_path,
            profile=review.profile,
            evidence=evidence,
            decision=review.decision,
        )
        if review.reviewed
        else ()
    )
    return SpecialistEvolutionCycle(review=review, experiments=experiments)


def evolve_default_specialists(
    db_path: str,
    *,
    trench_horizon_seconds: int = 3600,
) -> EcosystemEvolutionReview:
    """Review default specialists from newly resolved forward evidence.

    Reviews are idempotent with respect to unchanged evidence. Challenger experiments
    are registered only when a genuinely new evidence state is reviewed.
    """

    ensure_default_specialists(db_path)
    kalshi_evidence = kalshi_history_evidence(db_path)
    trench_evidence = trench1_evidence(
        db_path,
        horizon_seconds=trench_horizon_seconds,
    )
    return EcosystemEvolutionReview(
        kalshi=_review_with_challengers(
            db_path,
            specialist="kalshi-history",
            evidence=kalshi_evidence,
        ),
        trench=_review_with_challengers(
            db_path,
            specialist="trench-1",
            evidence=trench_evidence,
        ),
    )
