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
