from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite


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

    # This gate is a hard prerequisite, so malformed observations and policy
    # values must not turn into an eligible result through float comparison
    # behavior (notably NaN, for which every ordered comparison is false).
    policy_values = (
        ("maximum feed latency", policy.max_feed_latency_ms),
        ("maximum processing latency", policy.max_processing_latency_ms),
        ("maximum quote age", policy.max_quote_age_ms),
        ("minimum edge persistence", policy.min_edge_persistence_ms),
        ("maximum spread", policy.max_spread),
    )
    valid_policy: dict[str, bool] = {}
    for label, value in policy_values:
        valid_policy[label] = (
            isinstance(value, (int, float)) and isfinite(value) and value >= 0
        )
        if not valid_policy[label]:
            reasons.append(f"timing policy {label} is invalid")

    timestamps = (
        ("current time", state.now),
        ("market event time", state.last_market_event_at),
    )
    for label, value in timestamps:
        if not isinstance(value, datetime) or not _is_aware(value):
            reasons.append(f"{label} is missing a valid timezone")
    if (state.edge_first_seen_at is not None
            and (not isinstance(state.edge_first_seen_at, datetime)
                 or not _is_aware(state.edge_first_seen_at))):
        reasons.append("edge first-seen time is missing a valid timezone")

    for label, value in (
        ("feed latency", state.feed_latency_ms),
        ("processing latency", state.processing_latency_ms),
    ):
        if (not isinstance(value, (int, float)) or not isfinite(value)
                or value < 0):
            reasons.append(f"{label} is invalid")

    if state.spread is not None and (
        not isinstance(state.spread, (int, float)) or not isfinite(state.spread)
    ):
        reasons.append("spread is invalid")

    if not state.book_synchronized:
        reasons.append("orderbook unsynchronized")

    if (isinstance(state.now, datetime) and isinstance(state.last_market_event_at, datetime)
            and _is_aware(state.now) and _is_aware(state.last_market_event_at)):
        quote_age_ms = (state.now - state.last_market_event_at).total_seconds() * 1000
        if quote_age_ms < 0:
            reasons.append("market event timestamp in the future")
        elif (valid_policy["maximum quote age"]
              and quote_age_ms > policy.max_quote_age_ms):
            reasons.append(f"quote stale by {quote_age_ms:.0f}ms")

    if (isinstance(state.feed_latency_ms, (int, float))
            and isfinite(state.feed_latency_ms) and state.feed_latency_ms >= 0
            and valid_policy["maximum feed latency"]
            and state.feed_latency_ms > policy.max_feed_latency_ms):
        reasons.append(f"feed latency {state.feed_latency_ms:.0f}ms above limit")
    if (isinstance(state.processing_latency_ms, (int, float))
            and isfinite(state.processing_latency_ms) and state.processing_latency_ms >= 0
            and valid_policy["maximum processing latency"]
            and state.processing_latency_ms > policy.max_processing_latency_ms):
        reasons.append(
            f"processing latency {state.processing_latency_ms:.0f}ms above limit"
        )

    if state.spread is None:
        reasons.append("spread unavailable")
    elif (isinstance(state.spread, (int, float)) and isfinite(state.spread)
          and (state.spread < 0 or (valid_policy["maximum spread"]
                                   and state.spread > policy.max_spread))):
        reasons.append(f"spread {state.spread:.3f} outside timing policy")

    if state.edge_first_seen_at is None:
        reasons.append("edge persistence not established")
    elif (isinstance(state.now, datetime)
          and isinstance(state.edge_first_seen_at, datetime)
          and _is_aware(state.now) and _is_aware(state.edge_first_seen_at)):
        persistence_ms = (
            state.now - state.edge_first_seen_at
        ).total_seconds() * 1000
        if (valid_policy["minimum edge persistence"]
                and persistence_ms < policy.min_edge_persistence_ms):
            reasons.append(
                f"edge persisted only {persistence_ms:.0f}ms"
            )

    return TimingDecision(eligible=not reasons, reasons=tuple(reasons))


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
