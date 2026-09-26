from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median


@dataclass(frozen=True)
class LatencySample:
    event_at: datetime
    received_at: datetime
    processed_at: datetime

    @property
    def feed_latency_ms(self) -> float:
        return (self.received_at - self.event_at).total_seconds() * 1000

    @property
    def processing_latency_ms(self) -> float:
        return (self.processed_at - self.received_at).total_seconds() * 1000

    @property
    def total_latency_ms(self) -> float:
        return (self.processed_at - self.event_at).total_seconds() * 1000


@dataclass(frozen=True)
class LatencySummary:
    count: int
    median_feed_ms: float
    p95_feed_ms: float
    median_processing_ms: float
    p95_processing_ms: float


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


class LatencyMonitor:
    def __init__(self, max_samples: int = 10_000) -> None:
        self.max_samples = max_samples
        self.samples: list[LatencySample] = []

    def add(
        self,
        *,
        event_at: datetime,
        received_at: datetime | None = None,
        processed_at: datetime | None = None,
    ) -> LatencySample:
        received_at = received_at or datetime.now(UTC)
        processed_at = processed_at or datetime.now(UTC)
        for value in (event_at, received_at, processed_at):
            if value.tzinfo is None:
                raise ValueError("latency timestamps must be timezone-aware")
        sample = LatencySample(event_at, received_at, processed_at)
        self.samples.append(sample)
        if len(self.samples) > self.max_samples:
            self.samples = self.samples[-self.max_samples :]
        return sample

    def summary(self) -> LatencySummary:
        feed = [sample.feed_latency_ms for sample in self.samples]
        processing = [sample.processing_latency_ms for sample in self.samples]
        return LatencySummary(
            count=len(self.samples),
            median_feed_ms=median(feed) if feed else 0.0,
            p95_feed_ms=_percentile(feed, 0.95),
            median_processing_ms=median(processing) if processing else 0.0,
            p95_processing_ms=_percentile(processing, 0.95),
        )
