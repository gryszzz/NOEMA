from datetime import UTC, datetime, timedelta

from noema.timing_gate import TimingPolicy, TimingState, assess_timing


def test_fast_fresh_persistent_signal_is_eligible() -> None:
    now = datetime.now(UTC)
    result = assess_timing(
        TimingState(
            now=now,
            last_market_event_at=now - timedelta(milliseconds=100),
            feed_latency_ms=50,
            processing_latency_ms=5,
            edge_first_seen_at=now - timedelta(seconds=1),
            spread=0.02,
            book_synchronized=True,
        )
    )
    assert result.eligible is True


def test_stale_or_unsynchronized_signal_is_rejected() -> None:
    now = datetime.now(UTC)
    result = assess_timing(
        TimingState(
            now=now,
            last_market_event_at=now - timedelta(seconds=3),
            feed_latency_ms=1000,
            processing_latency_ms=5,
            edge_first_seen_at=now - timedelta(seconds=2),
            spread=0.02,
            book_synchronized=False,
        ),
        policy=TimingPolicy(max_quote_age_ms=1000),
    )
    assert result.eligible is False
    assert "orderbook unsynchronized" in result.reasons
