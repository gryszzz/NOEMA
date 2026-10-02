from noema.ledger import ForecastLedger
from noema.models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from noema.opportunity_radar import build_radar


def test_radar_reads_latest_forecast_rows(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger = ForecastLedger(db)
    snapshot = MarketSnapshot(
        venue="kalshi",
        market_id="M",
        title="Test market",
        yes_bid=0.50,
        yes_ask=0.54,
        no_bid=0.46,
        no_ask=0.50,
        liquidity_usd=5000,
        closes_at=None,
        resolution_rules="Rules",
    )
    forecast = Forecast(
        market_id="M",
        venue="kalshi",
        probability_yes=0.65,
        lower_bound=0.60,
        upper_bound=0.70,
        model_version="test",
    )
    opportunity = Opportunity(
        forecast=forecast,
        snapshot=snapshot,
        market_probability=0.54,
        raw_edge=0.11,
        estimated_cost=0.01,
        uncertainty_penalty=0.03,
        robust_edge=0.07,
    )
    action = Action(
        decision=Decision.PASS,
        market_id="M",
        venue="kalshi",
        max_price=None,
        stake_usd=0,
        reason="research",
    )
    ledger.append(snapshot, forecast, opportunity, action)

    rows = build_radar(db)
    assert len(rows) == 1
    assert rows[0].market_id == "M"
    assert rows[0].attention_score > 0


def test_negative_robust_edge_does_not_raise_attention(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger = ForecastLedger(db)
    snapshot = MarketSnapshot(
        venue="kalshi",
        market_id="NEG",
        title="Negative edge test",
        yes_bid=0.49,
        yes_ask=0.50,
        no_bid=0.50,
        no_ask=0.51,
        liquidity_usd=10000,
        closes_at=None,
        resolution_rules="Rules",
    )
    forecast = Forecast(
        market_id="NEG",
        venue="kalshi",
        probability_yes=0.40,
        lower_bound=0.35,
        upper_bound=0.45,
        model_version="test",
    )
    opportunity = Opportunity(
        forecast=forecast,
        snapshot=snapshot,
        market_probability=0.50,
        raw_edge=-0.10,
        estimated_cost=0.01,
        uncertainty_penalty=0.02,
        robust_edge=-0.13,
    )
    action = Action(
        decision=Decision.PASS,
        market_id="NEG",
        venue="kalshi",
        max_price=None,
        stake_usd=0,
        reason="negative edge",
    )
    ledger.append(snapshot, forecast, opportunity, action)
    row = build_radar(db)[0]
    assert row.attention_score < 0.70


def test_benchmark_rows_explain_missing_attention_score(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger = ForecastLedger(db)
    snapshot = MarketSnapshot(
        venue="kalshi", market_id="BASE", title="Test market",
        yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51,
        liquidity_usd=1000, closes_at=None, resolution_rules="Rules",
    )
    forecast = Forecast(
        market_id="BASE", venue="kalshi", probability_yes=0.50,
        lower_bound=0.50, upper_bound=0.50, model_version="market-baseline-v1",
    )
    opportunity = Opportunity(
        forecast=forecast, snapshot=snapshot, market_probability=0.50,
        raw_edge=0.0, estimated_cost=0.0, uncertainty_penalty=0.0, robust_edge=0.0,
    )
    action = Action(
        decision=Decision.PASS, market_id="BASE", venue="kalshi",
        max_price=None, stake_usd=0, reason="benchmark",
    )
    ledger.append(snapshot, forecast, opportunity, action)

    row = build_radar(db)[0]
    assert row.attention_score is None
    assert row.attention_score_suppression_reason == "benchmark_forecast_has_no_independent_edge"


def test_radar_reports_missing_quote_inputs_without_fabricating_values(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger = ForecastLedger(db)
    snapshot = MarketSnapshot(
        venue="kalshi", market_id="NO-BID", title="Quote coverage",
        yes_bid=None, yes_ask=0.51, no_bid=None, no_ask=0.50,
        liquidity_usd=None, closes_at=None, resolution_rules="Rules",
    )
    forecast = Forecast(
        market_id="NO-BID", venue="kalshi", probability_yes=0.60,
        lower_bound=0.55, upper_bound=0.65, model_version="test-model",
    )
    opportunity = Opportunity(
        forecast=forecast, snapshot=snapshot, market_probability=0.51,
        raw_edge=0.09, estimated_cost=0.02, uncertainty_penalty=0.02, robust_edge=0.05,
    )
    action = Action(
        decision=Decision.PASS, market_id="NO-BID", venue="kalshi",
        max_price=None, stake_usd=0, reason="incomplete quote",
    )
    ledger.append(snapshot, forecast, opportunity, action)

    row = build_radar(db)[0]
    assert row.yes_ask == 0.51
    assert row.spread is None
    assert "snapshot.yes_bid" in row.missing_inputs
