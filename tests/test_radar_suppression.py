from noema.ledger import ForecastLedger
from noema.models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from noema.opportunity_radar import build_radar


def test_political_market_attention_score_is_suppressed(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ledger = ForecastLedger(db)
    snapshot = MarketSnapshot(
        venue="kalshi",
        market_id="POL",
        title="Who will win the presidential election?",
        yes_bid=0.50,
        yes_ask=0.52,
        no_bid=0.48,
        no_ask=0.50,
        liquidity_usd=10000,
        closes_at=None,
        resolution_rules="Rules",
    )
    forecast = Forecast(
        market_id="POL",
        venue="kalshi",
        probability_yes=0.60,
        lower_bound=0.55,
        upper_bound=0.65,
        model_version="test",
    )
    opportunity = Opportunity(
        forecast=forecast,
        snapshot=snapshot,
        market_probability=0.52,
        raw_edge=0.08,
        estimated_cost=0.01,
        uncertainty_penalty=0.02,
        robust_edge=0.05,
    )
    ledger.append(
        snapshot,
        forecast,
        opportunity,
        Action(Decision.PASS, "POL", "kalshi", None, 0, "suppressed"),
    )

    rows = build_radar(db)
    assert rows[0].attention_score is None
