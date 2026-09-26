from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TimingPolicy:
    max_feed_latency_ms: float = 750.0
    max_processing_latency_ms: float = 100.0
    max_quote_age_ms: float = 1000.0
    min_edge_persistence_ms: float = 500.0
    max_spread: float = 0.06


@dataclass(frozen=True)
class TimingState:
    now: datetime
    last_market_event_at: datetime
    feed_latency_ms: float
    processing_latency_ms: float
    edge_first_seen_at: datetime | None
    spread: float | None
    book_synchronized: bool


@dataclass(frozen=True)
class TimingDecision:
    eligible: bool
    reasons: tuple[str, ...]


def assess_timing(
    state: TimingState,
    *,
    policy: TimingPolicy | None = None,
) -> TimingDecision:
    policy = policy or TimingPolicy()
    reasons: list[str] = []

    if not state.book_synchronized:
        reasons.append("orderbook unsynchronized")

    quote_age_ms = (state.now - state.last_market_event_at).total_seconds() * 1000
    if quote_age_ms < 0:
        reasons.append("market event timestamp in the future")
    elif quote_age_ms > policy.max_quote_age_ms:
        reasons.append(f"quote stale by {quote_age_ms:.0f}ms")

    if state.feed_latency_ms > policy.max_feed_latency_ms:
        reasons.append(f"feed latency {state.feed_latency_ms:.0f}ms above limit")
    if state.processing_latency_ms > policy.max_processing_latency_ms:
        reasons.append(
            f"processing latency {state.processing_latency_ms:.0f}ms above limit"
        )

    if state.spread is None:
        reasons.append("spread unavailable")
    elif state.spread < 0 or state.spread > policy.max_spread:
        reasons.append(f"spread {state.spread:.3f} outside timing policy")

    if state.edge_first_seen_at is None:
        reasons.append("edge persistence not established")
    else:
        persistence_ms = (
            state.now - state.edge_first_seen_at
        ).total_seconds() * 1000
        if persistence_ms < policy.min_edge_persistence_ms:
            reasons.append(
                f"edge persisted only {persistence_ms:.0f}ms"
            )

    return TimingDecision(eligible=not reasons, reasons=tuple(reasons))
