import json
from datetime import UTC, datetime, timedelta

import pytest

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
        resolved_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        raw={"result": "yes"},
    )
    summary = store.evaluate_ledger()
    assert summary.count == 1
    assert summary.mean_brier == pytest.approx(0.09)
    assert summary.mean_log_loss > 0

    row = store.conn.execute("SELECT raw_json FROM outcomes").fetchone()
    assert json.loads(row[0])["result"] == "yes"


def test_repeated_forecasts_count_once_and_past_settlement_does_not_score(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger = ForecastLedger(db)
    snapshot = MarketSnapshot(
        "kalshi", "KXTEST-ONE", "Test", .4, .5, .5, .6, 1000, None, "Rules",
    )
    forecast = Forecast("KXTEST-ONE", "kalshi", .7, .6, .8, "test")
    opportunity = Opportunity(forecast, snapshot, .5, .2, .02, .1, .08)
    action = Action(Decision.PASS, "KXTEST-ONE", "kalshi", None, 0, "test")
    ledger.append(snapshot, forecast, opportunity, action)
    ledger.append(snapshot, forecast, opportunity, action)
    outcomes = OutcomeStore(db)
    outcomes.upsert(
        venue="kalshi", market_id="KXTEST-ONE", outcome_yes=1,
        resolved_at=(snapshot.captured_at + timedelta(days=1)).isoformat(), raw={},
    )
    assert outcomes.evaluate_ledger().count == 1
    outcomes.upsert(
        venue="kalshi", market_id="KXTEST-ONE", outcome_yes=1,
        resolved_at=(snapshot.captured_at - timedelta(days=1)).isoformat(), raw={},
    )
    assert outcomes.evaluate_ledger().count == 0


def test_legacy_outcomes_gain_a_current_first_seen_timestamp(tmp_path) -> None:
    import sqlite3

    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE outcomes (venue TEXT, market_id TEXT, outcome_yes INTEGER, "
        "resolved_at TEXT, raw_json TEXT, PRIMARY KEY (venue, market_id))"
    )
    conn.execute(
        "INSERT INTO outcomes VALUES (?, ?, ?, ?, ?)",
        ("kalshi", "KXTEST-OLD", 1, "2020-01-01T00:00:00Z", "{}"),
    )
    conn.commit()
    store = OutcomeStore(db)
    seen = store.conn.execute("SELECT first_seen_at FROM outcomes").fetchone()[0]
    assert datetime.fromisoformat(seen) >= datetime.now(UTC) - timedelta(minutes=1)


def test_outcome_correction_updates_first_seen_but_repeat_sync_does_not(tmp_path) -> None:
    store = OutcomeStore(str(tmp_path / "noema.db"))
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    values = {
        "venue": "kalshi", "market_id": "KXTEST-OLD-A",
        "resolved_at": t0.isoformat(), "raw": {"event_ticker": "KXTEST-OLD"},
    }
    store.upsert(**values, outcome_yes=0, seen_at=t0 + timedelta(days=1))
    store.upsert(**values, outcome_yes=0, seen_at=t0 + timedelta(days=2))
    seen = store.conn.execute("SELECT first_seen_at FROM outcomes").fetchone()[0]
    assert seen == (t0 + timedelta(days=1)).isoformat()
    store.upsert(**values, outcome_yes=1, seen_at=t0 + timedelta(days=3))
    seen = store.conn.execute("SELECT first_seen_at FROM outcomes").fetchone()[0]
    assert seen == (t0 + timedelta(days=3)).isoformat()
