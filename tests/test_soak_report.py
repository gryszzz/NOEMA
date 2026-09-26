from noema.models import MarketSnapshot
from noema.soak import SoakStore
from noema.soak_report import build_soak_quality_report


def test_report_tracks_data_and_heartbeat_quality(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    store = SoakStore(db)
    store.append_market(
        MarketSnapshot(
            venue="kalshi",
            market_id="M1",
            title="Test",
            yes_bid=0.4,
            yes_ask=0.5,
            no_bid=0.5,
            no_ask=0.6,
            liquidity_usd=1000,
            closes_at=None,
            resolution_rules="Rules",
        )
    )
    store.heartbeat("kalshi:markets", ok=True)

    report = build_soak_quality_report(db)
    assert report.snapshots == 1
    assert report.valid_fraction == 1.0
    assert report.heartbeat_success_fraction == 1.0
