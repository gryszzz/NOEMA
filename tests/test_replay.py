from noema.models import MarketSnapshot
from noema.replay import SnapshotReplay
from noema.soak import SoakStore


def test_replay_returns_chronological_rows(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    store = SoakStore(db)
    for price in (0.40, 0.45):
        store.append_market(
            MarketSnapshot(
                venue="kalshi",
                market_id="M1",
                title="Test",
                yes_bid=price,
                yes_ask=price + 0.05,
                no_bid=0.50,
                no_ask=0.55,
                liquidity_usd=1000,
                closes_at=None,
                resolution_rules="Rules",
            )
        )

    rows = list(SnapshotReplay(db).iter_market("kalshi", "M1"))
    assert len(rows) == 2
    assert rows[0].snapshot["yes_bid"] == 0.40
    assert rows[1].snapshot["yes_bid"] == 0.45
