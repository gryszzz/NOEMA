from noema.experiment_factory import (
    admit_deferred_challengers,
    propose_challengers,
    register_challengers,
)
from noema.research_trials import ResearchTrialStore
from noema.specialist_evolution import SpecialistEvidence
from noema.specialists import SpecialistProfile, SpecialistState


def test_trench_trial_waits_for_full_walk_forward_evidence_floor() -> None:
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

    assert propose_challengers(profile, evidence, None) == ()

    qualified_profile = SpecialistProfile(
        name="trench-1",
        family="solana_new_tokens",
        state=SpecialistState.PAPER,
        resolved=70,
        reliability=0.2,
        calibration_error=None,
        after_cost_return=None,
        drawdown_fraction=None,
    )
    qualified_evidence = SpecialistEvidence(
        resolved=70,
        brier=None,
        market_baseline_brier=None,
        after_cost_return=None,
        max_drawdown_fraction=None,
        calibration_error=None,
        research_credible=None,
    )
    proposals = propose_challengers(qualified_profile, qualified_evidence, None)

    assert len(proposals) == 1
    assert proposals[0].family == "trench_survival"
    assert proposals[0].params["model"] == "logistic_baseline"


def test_unsupported_challengers_are_deferred_then_admitted_when_worker_exists(
    tmp_path, monkeypatch,
) -> None:
    profile = SpecialistProfile(
        name="kalshi-history",
        family="prediction_markets",
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

    db_path = str(tmp_path / "trials.db")
    generated = propose_challengers(profile, evidence, None)
    legacy_calibration = next(
        proposal for proposal in generated
        if proposal.family == "prediction_markets_calibration"
    )
    trial_store = ResearchTrialStore(db_path)
    legacy_id = trial_store.register(
        family=legacy_calibration.family,
        hypothesis=legacy_calibration.hypothesis,
        params=legacy_calibration.params,
        feature_set_version=legacy_calibration.feature_set_version,
        status="registered",
    )
    trial_store.conn.close()

    proposals = register_challengers(
        db_path, profile=profile, evidence=evidence, decision=None,
    )

    assert {proposal.status for proposal in proposals} == {"registered", "deferred"}
    trial_store = ResearchTrialStore(db_path)
    deferred = next(
        proposal for proposal in proposals
        if proposal.status == "deferred"
        and proposal.proposal.family == "prediction_markets_calibration"
    )
    deferred_trial = trial_store.get(deferred.trial_id)
    assert deferred_trial is not None
    assert deferred_trial.trial_id == legacy_id
    assert deferred_trial.status == "deferred"
    assert deferred_trial.hypothesis == deferred.proposal.hypothesis
    assert deferred_trial.params_json

    assert admit_deferred_challengers(db_path) == ()
    assert trial_store.get(deferred_trial.trial_id).status == "deferred"

    cost_trial = next(
        item for item in proposals
        if item.proposal.family == "prediction_markets_execution"
    )
    trial_store.set_status(cost_trial.trial_id, "rejected")
    repeated = register_challengers(
        db_path, profile=profile, evidence=evidence, decision=None,
    )
    reported_cost_trial = next(
        item for item in repeated if item.trial_id == cost_trial.trial_id
    )
    assert reported_cost_trial.status == "rejected"
    assert trial_store.get(cost_trial.trial_id).status == "rejected"

    def add_calibration_worker(family, feature_set_version, params):
        if (
            family == "prediction_markets_calibration"
            and feature_set_version == "calibration-v1"
            and params.get("experiment") == "calibration_challenger"
        ):
            return "kalshi-history", "calibration_challenger"
        return None

    monkeypatch.setattr(
        "noema.experiment_factory.handler_for_contract", add_calibration_worker,
    )
    admitted = admit_deferred_challengers(db_path)

    assert admitted == (deferred_trial.trial_id,)
    assert trial_store.get(deferred_trial.trial_id).status == "registered"


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
