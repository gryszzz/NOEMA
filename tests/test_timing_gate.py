from datetime import UTC, datetime, timedelta
from math import inf, nan

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


def test_non_finite_or_negative_measurements_fail_closed() -> None:
    now = datetime.now(UTC)
    base = {
        "now": now,
        "last_market_event_at": now - timedelta(milliseconds=100),
        "feed_latency_ms": 50,
        "processing_latency_ms": 5,
        "edge_first_seen_at": now - timedelta(seconds=1),
        "spread": 0.02,
        "book_synchronized": True,
    }

    for field, value, reason in (
        ("feed_latency_ms", nan, "feed latency is invalid"),
        ("processing_latency_ms", inf, "processing latency is invalid"),
        ("feed_latency_ms", -1, "feed latency is invalid"),
        ("spread", nan, "spread is invalid"),
    ):
        result = assess_timing(TimingState(**(base | {field: value})))
        assert result.eligible is False
        assert reason in result.reasons


def test_naive_timestamps_and_invalid_policy_fail_closed() -> None:
    now = datetime.now(UTC)
    state = TimingState(
        now=now,
        last_market_event_at=now - timedelta(milliseconds=100),
        feed_latency_ms=50,
        processing_latency_ms=5,
        edge_first_seen_at=now - timedelta(seconds=1),
        spread=0.02,
        book_synchronized=True,
    )
    naive_state = TimingState(
        **(state.__dict__ | {"last_market_event_at": now.replace(tzinfo=None)})
    )
    result = assess_timing(naive_state)
    assert result.eligible is False
    assert "market event time is missing a valid timezone" in result.reasons

    result = assess_timing(state, policy=TimingPolicy(max_quote_age_ms=nan))
    assert result.eligible is False
    assert "timing policy maximum quote age is invalid" in result.reasons
