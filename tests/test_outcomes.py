import pytest

import json

from noema.ledger import ForecastLedger
from noema.models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from noema.outcomes import OutcomeStore


def test_outcomes_join_forecast_ledger(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger = ForecastLedger(db)
    market = MarketSnapshot(
        venue="kalshi",
        market_id="KXTEST",
        title="Test",
        yes_bid=0.50,
        yes_ask=0.55,
        no_bid=0.44,
        no_ask=0.49,
        liquidity_usd=10000,
        closes_at=None,
        resolution_rules="Test rules",
    )
    forecast = Forecast(
        market_id="KXTEST",
        venue="kalshi",
        probability_yes=0.70,
        lower_bound=0.65,
        upper_bound=0.75,
        model_version="unit",
    )
    opportunity = Opportunity(
        forecast=forecast,
        snapshot=market,
        market_probability=0.55,
        raw_edge=0.15,
        estimated_cost=0.02,
        uncertainty_penalty=0.05,
        robust_edge=0.08,
    )
    action = Action(
        decision=Decision.PAPER_BUY_YES,
        market_id="KXTEST",
        venue="kalshi",
        max_price=0.55,
        stake_usd=5.0,
        reason="unit",
    )
    ledger.append(market, forecast, opportunity, action)

    store = OutcomeStore(db)
    store.upsert(
        venue="kalshi",
        market_id="KXTEST",
        outcome_yes=1,
        resolved_at="2026-01-01T00:00:00Z",
        raw={"result": "yes"},
    )
    summary = store.evaluate_ledger()
    assert summary.count == 1
    assert summary.mean_brier == pytest.approx(0.09)
    assert summary.mean_log_loss > 0

    row = store.conn.execute("SELECT raw_json FROM outcomes").fetchone()
    assert json.loads(row[0])["result"] == "yes"
