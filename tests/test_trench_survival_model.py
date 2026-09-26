from datetime import UTC, datetime, timedelta

from noema.trench_survival_model import (
    MODEL_VERSION,
    TrenchSurvivalExample,
    audit_examples,
    fit_logistic,
    predict_survival,
)


def example(index: int, survived: int) -> TrenchSurvivalExample:
    captured = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index * 2)
    good = bool(survived)
    return TrenchSurvivalExample(
        candidate_id=f"candidate-{index}",
        captured_at=captured,
        label_observed_at=captured + timedelta(hours=1),
        features={
            "return_fraction": 1.5 if good else -0.4,
            "max_drawdown_fraction": 0.05 if good else 0.55,
            "liquidity_growth_fraction": 2.0 if good else -0.4,
            "signed_flow_imbalance": 0.7 if good else -0.6,
            "buyer_growth_fraction": 5.0 if good else 0.2,
            "buyer_acceleration": 0.5 if good else -0.3,
            "participation_balance": 0.7 if good else 0.2,
            "organic_buyer_share": 0.7 if good else 0.1,
            "organic_volume_fraction": 0.8 if good else 0.15,
            "top_holder_fraction": 0.07 if good else 0.35,
            "top5_holder_fraction": 0.25 if good else 0.70,
            "holder_hhi": 0.04 if good else 0.30,
            "creator_supply_fraction": 0.02 if good else 0.20,
            "organic_score": 85 if good else 15,
        },
        control={
            "token_program": "Token",
            "mint_authority_present": False if good else True,
            "freeze_authority_present": False,
            "permanent_delegate_present": False,
            "transfer_hook_present": False,
            "transfer_fee_bps": 0,
            "suspicious_flag": False,
        },
        survived=survived,
    )


def test_logistic_model_learns_a_simple_survival_signal() -> None:
    training = [example(index, index % 2) for index in range(60)]
    weights = fit_logistic(training)

    good = predict_survival(example(100, 1), weights)
    bad = predict_survival(example(101, 0), weights)

    assert good > bad
    assert good > 0.5
    assert bad < 0.5


def test_walk_forward_survival_audit_beats_constant_baseline_on_signal() -> None:
    examples = [example(index, index % 2) for index in range(100)]

    audit = audit_examples(examples, min_train=30, min_test=20)

    assert audit.model_version == MODEL_VERSION
    assert audit.status == "research_review_required"
    assert audit.walk_forward_tests >= 20
    assert audit.model_brier is not None
    assert audit.baseline_brier is not None
    assert audit.model_brier < audit.baseline_brier
    assert audit.paper_forecast_eligible is True
    assert audit.live_eligible is False
    assert audit.weights is not None


def test_survival_audit_refuses_small_samples() -> None:
    audit = audit_examples(
        [example(index, index % 2) for index in range(20)],
        min_train=15,
        min_test=10,
    )

    assert audit.status == "insufficient_forward_labels"
    assert audit.live_eligible is False
