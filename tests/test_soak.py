from noema.models import MarketSnapshot
from noema.soak import SoakStore


def market(market_id: str = "M1") -> MarketSnapshot:
    return MarketSnapshot(
        venue="kalshi",
        market_id=market_id,
        title="Will the test pass?",
        yes_bid=0.40,
        yes_ask=0.45,
        no_bid=0.55,
        no_ask=0.60,
        liquidity_usd=1000,
        closes_at=None,
        resolution_rules="Resolves YES if the test passes.",
    )


def test_soak_store_appends_and_reports(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    store = SoakStore(db)
    result = store.append_market(market())

    assert result.valid is True
    stats = store.stats()
    assert stats.snapshots == 1
    assert stats.valid_snapshots == 1
    assert stats.invalid_snapshots == 0
    assert stats.distinct_markets == 1


def test_soak_store_preserves_invalid_rows(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    store = SoakStore(db)
    broken = MarketSnapshot(
        venue="kalshi",
        market_id="BROKEN",
        title="Broken",
        yes_bid=0.90,
        yes_ask=0.10,
        no_bid=0.10,
        no_ask=0.90,
        liquidity_usd=1000,
        closes_at=None,
        resolution_rules="Rules",
    )
    result = store.append_market(broken)

    assert result.valid is False
    assert store.stats().invalid_snapshots == 1
