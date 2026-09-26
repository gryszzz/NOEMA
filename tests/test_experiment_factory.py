from noema.experiment_factory import propose_challengers
from noema.specialist_evolution import SpecialistEvidence
from noema.specialists import SpecialistProfile, SpecialistState


def test_trench_labels_create_simple_survival_challengers() -> None:
    profile = SpecialistProfile(
        name="trench-1",
        family="solana_new_tokens",
        state=SpecialistState.PAPER,
        resolved=30,
        reliability=0.2,
        calibration_error=None,
        after_cost_return=None,
        drawdown_fraction=None,
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

    proposals = propose_challengers(profile, evidence, None)

    assert proposals
    assert proposals[0].family == "trench_survival"
    assert len(proposals) <= 3


def test_experiment_factory_reacts_to_calibration_and_cost_failure() -> None:
    profile = SpecialistProfile(
        name="candidate",
        family="prediction",
        state=SpecialistState.PAPER,
        resolved=150,
        reliability=0.4,
        calibration_error=0.15,
        after_cost_return=-0.01,
        drawdown_fraction=0.04,
    )
    evidence = SpecialistEvidence(
        resolved=150,
        brier=0.24,
        market_baseline_brier=0.22,
        after_cost_return=-0.01,
        max_drawdown_fraction=0.04,
        calibration_error=0.15,
        research_credible=None,
    )

    proposals = propose_challengers(profile, evidence, None)
    families = {proposal.family for proposal in proposals}

    assert "prediction_calibration" in families
    assert "prediction_execution" in families or "prediction_ablation" in families
