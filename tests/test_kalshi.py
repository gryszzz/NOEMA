from noema.config import KalshiConfig
from noema.venues.kalshi import KalshiVenue


def test_demo_is_default_environment() -> None:
    config = KalshiConfig()
    assert config.environment == "demo"
    assert "demo.kalshi.co" in config.base_url
    assert config.allow_live_orders is False


def test_market_mapping_uses_fixed_point_dollars() -> None:
    raw = {
        "ticker": "TEST-YES",
        "title": "Will the test pass?",
        "yes_bid_dollars": "0.4300",
        "yes_ask_dollars": "0.4700",
        "yes_bid_size_fp": "100.00",
        "yes_ask_size_fp": "50.00",
        "no_bid_dollars": "0.5300",
        "no_ask_dollars": "0.5700",
        "close_time": "2026-10-01T12:00:00Z",
        "rules_primary": "Resolves YES if the test passes.",
    }

    market = KalshiVenue._market_snapshot(raw)

    assert market is not None
    assert market.market_id == "TEST-YES"
    assert market.yes_bid == 0.43
    assert market.yes_ask == 0.47
    assert market.no_bid == 0.53
    assert market.resolution_rules == "Resolves YES if the test passes."
    assert market.liquidity_usd == 66.5
