from datetime import UTC, datetime, timedelta

import pytest

from noema.history_forecaster import MODEL_VERSION
from noema.ledger import ForecastLedger
from noema.models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from noema.outcomes import OutcomeStore
from noema.paired_evaluation import compare_history_to_market


def test_two_sides_of_one_event_do_not_double_weight_the_comparison(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger, outcomes = ForecastLedger(db), OutcomeStore(db)
    now = datetime.now(UTC)
    for ticker, baseline, result in (
        ("KXTEST-EVENT1-A", .9, 1),
        ("KXTEST-EVENT1-B", .1, 0),
        ("KXTEST-EVENT2-A", .1, 1),
    ):
        market = MarketSnapshot(
            "kalshi:demo", ticker, "Question", .3, baseline, .1, .6,
            1000, None, "Rules", captured_at=now,
        )
        for version, p in (("market-baseline-v1", baseline), (MODEL_VERSION, .5)):
            forecast = Forecast(ticker, "kalshi:demo", p, p, p, version)
            opportunity = Opportunity(forecast, market, baseline, 0, 0, 0, 0)
            action = Action(Decision.PASS, ticker, "kalshi:demo", None, 0, "test")
            ledger.append(market, forecast, opportunity, action)
        outcomes.upsert(
            venue="kalshi:demo", market_id=ticker, outcome_yes=result,
            resolved_at=(now + timedelta(days=1)).isoformat(), raw={},
        )
    comparison = compare_history_to_market(db)
    assert comparison.distinct_resolved_markets == 3
    assert comparison.distinct_resolved_events == 2
    assert comparison.candidate_brier == pytest.approx(.25)
    assert comparison.market_baseline_brier == pytest.approx(.41)
