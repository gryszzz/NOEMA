from noema.baseline_recording import record_market_baseline
from noema.ledger import ForecastLedger
from noema.models import MarketSnapshot
from noema.opportunity_radar import build_radar


def test_collected_baseline_is_scored_as_research_but_never_ranked_as_edge(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    market = MarketSnapshot(
        venue="kalshi", market_id="TEST", title="Will the test pass?",
        yes_bid=0.48, yes_ask=0.50, no_bid=0.50, no_ask=0.52,
        liquidity_usd=12000, closes_at=None, resolution_rules="Test resolution",
    )
    record_market_baseline(market, ForecastLedger(db))
    rows = build_radar(db)
    assert len(rows) == 1
    assert rows[0].probability_yes == 0.50
    assert rows[0].decision == "pass"
    assert rows[0].attention_score is None
