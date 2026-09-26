from __future__ import annotations

from dataclasses import asdict, dataclass

from .ecosystem_store import EcosystemStore
from .evolution_store import EvolutionStore
from .specialist_evolution import (
    EvolutionDecision,
    EvolutionPolicy,
    SpecialistEvidence,
    apply_evolution,
    evolve_specialist,
)
from .specialists import SpecialistProfile


@dataclass(frozen=True)
class SpecialistReviewResult:
    specialist: str
    reviewed: bool
    profile: SpecialistProfile
    decision: EvolutionDecision | None
    detail: str


def review_specialist(
    db_path: str,
    *,
    specialist: str,
    evidence: SpecialistEvidence,
    policy: EvolutionPolicy | None = None,
) -> SpecialistReviewResult:
    ecosystem = EcosystemStore(db_path)
    profiles = {profile.name: profile for profile in ecosystem.profiles()}
    if specialist not in profiles:
        raise KeyError(specialist)

    profile = profiles[specialist]
    evolution = EvolutionStore(db_path)
    latest = evolution.latest_review(specialist)
    evidence_payload = asdict(evidence)

    if latest is not None and latest.get("evidence") == evidence_payload:
        return SpecialistReviewResult(
            specialist=specialist,
            reviewed=False,
            profile=profile,
            decision=None,
            detail="evidence unchanged; review streak not advanced",
        )

    state = evolution.state(specialist)
    decision = evolve_specialist(
        profile,
        evidence,
        success_streak=state.success_streak,
        failure_streak=state.failure_streak,
        policy=policy,
    )
    updated = apply_evolution(profile, evidence, decision)
    ecosystem.upsert_profile(updated)
    evolution.record_review(
        specialist=specialist,
        evidence=evidence,
        decision=decision,
    )
    return SpecialistReviewResult(
        specialist=specialist,
        reviewed=True,
        profile=updated,
        decision=decision,
        detail="specialist evidence reviewed and registry updated",
    )
