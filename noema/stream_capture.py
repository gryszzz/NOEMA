from __future__ import annotations

from dataclasses import dataclass

from .realtime_journal import RealtimeJournal
from .venues.kalshi_stream import KalshiStream


@dataclass(frozen=True)
class CaptureResult:
    messages: int


async def capture_stream(
    stream: KalshiStream,
    journal: RealtimeJournal,
    *,
    channels: list[str],
    market_tickers: list[str] | None = None,
    max_messages: int | None = None,
) -> CaptureResult:
    count = 0
    async for message in stream.messages(
        channels=channels,
        market_tickers=market_tickers,
    ):
        journal.append(
            channel=message.type,
            payload=message.payload,
            received_at=message.received_at,
        )
        count += 1
        if max_messages is not None and count >= max_messages:
            break
    return CaptureResult(messages=count)
