from datetime import UTC, datetime

import pytest

from noema.realtime_journal import RealtimeJournal
from noema.stream_capture import capture_stream
from noema.venues.kalshi_stream import KalshiStreamMessage


class FakeStream:
    async def messages(self, **kwargs):
        del kwargs
        yield KalshiStreamMessage(
            type="ticker",
            payload={"type": "ticker", "msg": {"market_ticker": "M"}},
            received_at=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_capture_stream_persists_raw_frame(tmp_path) -> None:
    journal = RealtimeJournal(str(tmp_path / "noema.db"))
    result = await capture_stream(
        FakeStream(),
        journal,
        channels=["ticker"],
        max_messages=1,
    )
    assert result.messages == 1
    row = journal.conn.execute(
        "SELECT channel, payload_json FROM realtime_events"
    ).fetchone()
    assert row[0] == "ticker"
    assert "market_ticker" in row[1]
