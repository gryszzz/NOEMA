import pytest

from noema.config import KalshiConfig
from noema.outcomes import OutcomeStore
from noema.sync import sync_kalshi_outcomes


class FakeHistory:
    config = KalshiConfig(environment="demo")
    async def settled_page(self, partition, *, cursor, limit):
        return ([
            {"ticker": "BAD", "result": "yes", "settlement_ts": None},
            {"ticker": "GOOD", "result": "no", "settlement_ts": "2026-01-01T00:00:00Z"},
        ], None) if partition == "live" else ([], None)


@pytest.mark.asyncio
async def test_sync_skips_outcomes_without_reliable_settlement_time(tmp_path) -> None:
    store = OutcomeStore(str(tmp_path / "noema.db"))
    result = await sync_kalshi_outcomes(store, FakeHistory())
    assert (result.scanned, result.imported, result.skipped) == (2, 1, 1)
    tickers = store.conn.execute("SELECT venue, market_id FROM outcomes").fetchall()
    assert tickers == [("kalshi:demo", "GOOD")]
