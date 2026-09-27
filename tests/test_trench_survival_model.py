import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from noema.trench_survival_model import (
    MODEL_VERSION,
    TrenchSurvivalExample,
    _fingerprint,
    audit_examples,
    fit_logistic,
    load_verified_examples,
    predict_survival,
    vectorize_example,
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
            "mint_authority_present": not good,
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


def test_replaying_candidates_cannot_satisfy_sample_gate() -> None:
    rows = [example(index, index % 2) for index in range(20)]
    audit = audit_examples(rows * 5, min_train=15, min_test=10)
    assert audit.status == "insufficient_forward_labels"
    assert audit.total_labels == 20
    assert audit.walk_forward_tests == 0
    assert audit.paper_forecast_eligible is False


def test_conflicting_candidate_evidence_is_rejected() -> None:
    row = example(0, 0)
    with pytest.raises(ValueError, match="conflicting"):
        audit_examples([row, replace(row, survived=1)])


def test_labels_unavailable_at_forecast_time_are_purged() -> None:
    rows = [
        replace(example(index, index % 2), label_observed_at=example(100, 1).captured_at)
        for index in range(6)
    ]
    audit = audit_examples(rows, min_train=2, min_test=2)
    assert audit.walk_forward_tests == 0
    assert audit.status == "insufficient_walk_forward_tests"


@pytest.mark.parametrize("field", ["return_fraction", "organic_score"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_feature_cannot_be_clipped_into_apparent_signal(field, value) -> None:
    row = example(0, 0)
    with pytest.raises(ValueError, match="non-finite"):
        vectorize_example(replace(row, features={**row.features, field: value}))


def test_audit_cache_identity_includes_features_control_and_capture_time() -> None:
    row = example(0, 0)
    fingerprint = _fingerprint([row])
    for changed in (
        replace(row, features={**row.features, "return_fraction": 0.1}),
        replace(row, control={**row.control, "mint_authority_present": False}),
        replace(row, captured_at=row.captured_at - timedelta(seconds=1)),
    ):
        assert _fingerprint([changed]) != fingerprint
    assert _fingerprint([row, example(1, 1)]) == _fingerprint([example(1, 1), row])


@pytest.fixture
def survival_db(tmp_path):
    path = str(tmp_path / "survival.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trench_candidates (
            candidate_id TEXT, token_mint TEXT, captured_at TEXT,
            reference_liquidity_usd REAL, assessment_json TEXT
        );
        CREATE TABLE trench_observations (
            mint TEXT, horizon_seconds INTEGER, scheduled_at TEXT, observed_at TEXT,
            tick_json TEXT, control_json TEXT
        );
        CREATE TABLE trench_counterfactuals (
            candidate_id TEXT, horizon_seconds INTEGER, max_drawdown_fraction REAL,
            observed_at TEXT
        );
    """)
    row = example(0, 1)
    captured = row.captured_at.isoformat()
    observed = row.label_observed_at.isoformat()
    conn.execute(
        "INSERT INTO trench_candidates VALUES (?, 'mint', ?, 100, ?)",
        (row.candidate_id, captured, json.dumps({"features": row.features})),
    )
    control = json.dumps(row.control)
    tick = json.dumps({"price_usd": 1, "liquidity_usd": 100})
    conn.execute(
        "INSERT INTO trench_observations VALUES ('mint', 300, ?, ?, ?, ?)",
        (captured, captured, tick, control),
    )
    conn.execute(
        "INSERT INTO trench_observations VALUES ('mint', 3600, ?, ?, ?, ?)",
        ((row.captured_at + timedelta(seconds=3300)).isoformat(), observed, tick, control),
    )
    conn.execute(
        "INSERT INTO trench_counterfactuals VALUES (?, 3600, 0.1, ?)",
        (row.candidate_id, observed),
    )
    conn.commit()
    yield path, conn
    conn.close()


def test_loader_requires_matching_feature_and_label_evidence(survival_db) -> None:
    path, _ = survival_db
    rows = load_verified_examples(path)
    assert len(rows) == 1
    assert rows[0].survived == 1


@pytest.mark.parametrize("change", [
    ("UPDATE trench_observations SET observed_at='2026-01-01T00:01:00+00:00' "
    "WHERE horizon_seconds=300"),
    ("UPDATE trench_observations SET scheduled_at='2026-01-01T02:00:00+00:00' "
    "WHERE horizon_seconds=3600"),
    "UPDATE trench_counterfactuals SET observed_at='2026-01-01T02:00:00+00:00'",
    "UPDATE trench_observations SET tick_json='{}' WHERE horizon_seconds=3600",
    "UPDATE trench_counterfactuals SET max_drawdown_fraction=2",
    "UPDATE trench_observations SET control_json='{}' WHERE horizon_seconds=3600",
])
def test_loader_rejects_temporal_leakage_and_incomplete_outcomes(survival_db, change) -> None:
    path, conn = survival_db
    conn.execute(change)
    conn.commit()
    assert load_verified_examples(path) == []


def test_later_assessment_cannot_replace_an_unlabelled_first_candidate(survival_db) -> None:
    path, conn = survival_db
    conn.execute("""
        INSERT INTO trench_candidates
        SELECT 'earliest', token_mint, '2025-12-31T23:59:00+00:00',
               reference_liquidity_usd, assessment_json
        FROM trench_candidates
    """)
    conn.commit()
    assert load_verified_examples(path) == []


def test_survival_audit_refuses_small_samples() -> None:
    audit = audit_examples(
        [example(index, index % 2) for index in range(20)],
        min_train=15,
        min_test=10,
    )

    assert audit.status == "insufficient_forward_labels"
    assert audit.live_eligible is False
