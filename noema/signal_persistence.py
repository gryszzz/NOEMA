from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class PersistenceState:
    threshold: float
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    peak_edge: float = 0.0
    observations: int = 0

    def observe(self, edge: float, *, observed_at: datetime | None = None) -> None:
        observed_at = observed_at or datetime.now(UTC)
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

        if edge < self.threshold:
            self.reset()
            return

        if self.first_seen_at is None:
            self.first_seen_at = observed_at
        self.last_seen_at = observed_at
        self.peak_edge = max(self.peak_edge, edge)
        self.observations += 1

    def reset(self) -> None:
        self.first_seen_at = None
        self.last_seen_at = None
        self.peak_edge = 0.0
        self.observations = 0

    def age_ms(self, *, now: datetime | None = None) -> float:
        if self.first_seen_at is None:
            return 0.0
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.first_seen_at).total_seconds() * 1000)
