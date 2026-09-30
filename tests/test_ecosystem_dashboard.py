from noema.ecosystem_controller import ensure_default_specialists
from noema.ecosystem_dashboard import build_ecosystem_overview
from noema.ecosystem_evolution import _review_with_challengers, evolve_default_specialists
from noema.evolution_controller import SpecialistReviewResult
from noema.research_trials import ResearchTrialStore
from noema.specialist_evolution import SpecialistEvidence
from noema.specialists import SpecialistProfile, SpecialistState


def test_ecosystem_overview_exposes_profiles_reviews_and_trials(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ensure_default_specialists(db)
    evolve_default_specialists(db)

    overview = build_ecosystem_overview(db)

    assert overview["database_present"] is True
    names = {row["name"] for row in overview["specialists"]}
    assert {"kalshi-history", "trench-1"} <= names
    assert overview["latest_plan"] is None


def test_evolution_cycle_admits_old_deferred_trials_without_new_evidence(
    tmp_path, monkeypatch,
) -> None:
    db = str(tmp_path / "noema.db")
    trials = ResearchTrialStore(db)
    trial_id = trials.register(
        family="prediction_markets_calibration",
        hypothesis="A persisted calibration hypothesis",
        params={"experiment": "calibration_challenger"},
        feature_set_version="calibration-v1",
        status="deferred",
    )
    monkeypatch.setattr(
        "noema.experiment_factory.handler_for_contract",
        lambda family, version, params: (
            ("kalshi-history", "calibration_challenger")
            if family == "prediction_markets_calibration"
            and version == "calibration-v1"
            else None
        ),
    )

    result = evolve_default_specialists(db)

    assert result.admitted_trial_ids == (trial_id,)
    assert trials.get(trial_id).status == "registered"


def test_unchanged_evidence_recreates_durable_deferred_proposals_idempotently(
    tmp_path, monkeypatch,
) -> None:
    db = str(tmp_path / "noema.db")
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
    monkeypatch.setattr(
        "noema.ecosystem_evolution.review_specialist",
        lambda *args, **kwargs: SpecialistReviewResult(
            "kalshi-history", False, profile, None, "evidence unchanged",
        ),
    )

    first = _review_with_challengers(
        db, specialist="kalshi-history", evidence=evidence,
    )
    second = _review_with_challengers(
        db, specialist="kalshi-history", evidence=evidence,
    )
    trials = ResearchTrialStore(db)

    assert first.review.reviewed is False
    assert [item.trial_id for item in first.experiments] == [
        item.trial_id for item in second.experiments
    ]
    assert trials.count_family("prediction_markets_ablation") == 1
    assert trials.count_family("prediction_markets_calibration") == 1
    assert trials.recent(status="deferred", limit=10)
