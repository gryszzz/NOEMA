from dataclasses import replace

from noema.models import MarketSnapshot
from noema.validation import validate_market_snapshot


def valid_market() -> MarketSnapshot:
    return MarketSnapshot(
        venue="kalshi",
        market_id="TEST",
        title="Will X happen?",
        yes_bid=0.40,
        yes_ask=0.45,
        no_bid=0.55,
        no_ask=0.60,
        liquidity_usd=1000,
        closes_at=None,
        resolution_rules="Resolves YES if X happens.",
    )


def test_valid_market_passes() -> None:
    result = validate_market_snapshot(valid_market())
    assert result.valid is True
    assert result.issues == ()


def test_out_of_range_price_rejected() -> None:
    result = validate_market_snapshot(replace(valid_market(), yes_ask=1.2))
    assert result.valid is False
    assert "yes_ask outside [0, 1]" in result.issues


def test_crossed_book_rejected() -> None:
    result = validate_market_snapshot(
        replace(valid_market(), yes_bid=0.60, yes_ask=0.50)
    )
    assert result.valid is False
    assert "YES book crossed" in result.issues


def test_missing_rules_rejected() -> None:
    result = validate_market_snapshot(replace(valid_market(), resolution_rules=None))
    assert result.valid is False
    assert "missing resolution rules" in result.issues
