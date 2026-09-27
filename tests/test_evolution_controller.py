from dataclasses import replace

from noema.ecosystem_store import EcosystemStore
from noema.evolution_controller import review_specialist
from noema.evolution_store import EvolutionStore
from noema.specialist_evolution import SpecialistEvidence
from noema.specialists import SpecialistState


def test_unchanged_evidence_does_not_advance_review_streak(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ecosystem = EcosystemStore(db)
    ecosystem.ensure_specialist(
        name="trench-1",
        family="solana_new_tokens",
        state=SpecialistState.SHADOW,
    )
    evidence = SpecialistEvidence(
        resolved=30,
        brier=None,
        market_baseline_brier=None,
        after_cost_return=None,
        max_drawdown_fraction=None,
        calibration_error=None,
        research_credible=None,
    )

    first = review_specialist(
        db,
        specialist="trench-1",
        evidence=evidence,
    )
    second = review_specialist(
        db,
        specialist="trench-1",
        evidence=evidence,
    )

    assert first.reviewed is True
    assert first.profile.state is SpecialistState.PAPER
    assert second.reviewed is False
    assert EvolutionStore(db).state("trench-1").review_count == 1


def strong_evidence() -> SpecialistEvidence:
    return SpecialistEvidence(150, 0.18, 0.22, 0.04, 0.04, 0.04, True, 0.2, 0.9)


def test_metric_revisions_cannot_promote_without_new_resolutions(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    EcosystemStore(db).ensure_specialist(
        name="test", family="test", state=SpecialistState.PAPER,
    )
    evidence = strong_evidence()
    first = review_specialist(db, specialist="test", evidence=evidence)
    assert first.decision.success_streak == 1

    for revised in (
        replace(evidence, after_cost_return=0.05),
        evidence,  # Alternating previously seen aggregates is not fresh evidence.
        replace(evidence, resolved=149),
        replace(evidence, after_cost_return=0.06),
    ):
        result = review_specialist(db, specialist="test", evidence=revised)
        assert result.profile.state is SpecialistState.PAPER
        assert result.decision.success_streak == 1
        assert "no newly resolved evidence" in " ".join(result.decision.reasons)

    fresh = review_specialist(
        db, specialist="test", evidence=replace(evidence, resolved=151),
    )
    assert fresh.profile.state is SpecialistState.ACTIVE_RESEARCH


def test_hard_failure_is_applied_without_new_resolutions(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    EcosystemStore(db).ensure_specialist(
        name="test", family="test", state=SpecialistState.PAPER,
    )
    evidence = strong_evidence()
    review_specialist(db, specialist="test", evidence=evidence)
    failed = review_specialist(
        db, specialist="test", evidence=replace(evidence, max_drawdown_fraction=0.3),
    )
    assert failed.profile.state is SpecialistState.QUARANTINED
    assert failed.decision.hard_quarantine is True
    for i in range(4):
        result = review_specialist(
            db, specialist="test", evidence=replace(evidence, after_cost_return=0.05 + i / 100),
        )
        assert result.profile.state is SpecialistState.QUARANTINED
        assert result.decision.success_streak == 0
