from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from noema.baseline_recording import record_market_baseline
from noema.ledger import ForecastLedger
from noema.models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from noema.outcomes import OutcomeStore
from noema.specialist_model import (
    Example,
    audit_database,
    audit_series,
    fit_calibration,
    load_verified_examples,
    predict_calibration,
)


def _examples(count: int, *, late_seen: bool = False) -> list[Example]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        captured = start + timedelta(days=index)
        rows.append(Example(
            "kalshi:production", "SERIES", f"SERIES-{index}",
            f"SERIES-{index}-A", captured, captured + timedelta(hours=2),
            captured + timedelta(days=200 if late_seen else 0, hours=3),
            0.7, index % 2,
        ))
    return rows


def test_model_audit_fails_closed_without_enough_resolved_events() -> None:
    report = audit_series(_examples(20), min_train=10, min_test=15)
    assert report.status == "insufficient_resolved_events"
    assert report.deployment_eligible is False


def test_walk_forward_refuses_training_outcomes_seen_after_snapshot() -> None:
    report = audit_series(_examples(45, late_seen=True), min_train=10, min_test=10)
    assert report.status == "insufficient_walk_forward_tests"
    assert report.test_events == 0


def test_learned_calibration_compares_with_market_on_later_events() -> None:
    report = audit_series(_examples(60), min_train=20, min_test=15)
    assert report.status == "research_review_required"
    assert report.test_events >= 15
    assert report.model_brier is not None and report.market_brier is not None
    assert report.model_brier < report.market_brier
    assert report.deployment_eligible is False
    assert 0 < predict_calibration(0.7, (0.0, 1.0, 0.0), 0.5) < 1
    with pytest.raises(ValueError):
        predict_calibration(float("nan"), (0.0, 1.0, 0.0), 0.5)


def test_model_can_learn_from_independent_history_when_quote_is_flat() -> None:
    rows = [
        Example(
            row.venue, row.series, row.event, row.ticker, row.captured_at,
            row.settled_at, row.first_seen_at, 0.5, row.outcome,
            0.8 if row.outcome else 0.2,
        )
        for row in _examples(55)
    ]
    weights = fit_calibration(rows[:20])
    assert weights[2] > 0
    report = audit_series(rows, min_train=20, min_test=15)
    assert report.status == "research_review_required"
    assert report.market_brier == pytest.approx(0.25)
    assert report.model_brier < report.market_brier


def test_database_requires_verified_matching_snapshot_and_later_outcome(tmp_path) -> None:
    path = str(tmp_path / "model.db")
    ledger, outcomes = ForecastLedger(path), OutcomeStore(path)
    captured = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(2):
        ticker = f"SERIES-{index}-A"
        market = MarketSnapshot(
            "kalshi:production", ticker, "test", 0.4, 0.5, 0.5, 0.6,
            100.0, captured + timedelta(days=1), "settles yes/no", captured,
        )
        record_market_baseline(market, ledger)
        if index == 0:
            forecast = Forecast(ticker, market.venue, 0.5, 0.3, 0.7, "series-frequency-v1")
            ledger.append(
                market, forecast, Opportunity(forecast, market, 0.5, 0, 0, 0, 0),
                Action(Decision.PASS, ticker, market.venue, None, 0, "paper"),
            )
        outcomes.upsert(
            venue=market.venue, market_id=ticker, outcome_yes=index,
            resolved_at=(captured + timedelta(days=1)).isoformat(),
            raw={"event_ticker": f"SERIES-{index}"},
            seen_at=captured + timedelta(days=2),
        )
    loaded = load_verified_examples(path)
    assert [row.ticker for row in loaded] == ["SERIES-0-A"]
    assert loaded[0].history_probability == 0.5
    assert audit_database(path)["kalshi:production/SERIES"]["status"] == (
        "insufficient_resolved_events"
    )
