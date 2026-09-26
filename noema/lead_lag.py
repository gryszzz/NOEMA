from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class LeadLagResult:
    best_lag: int | None
    correlation: float
    observations: int


def _corr(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 3:
        return 0.0
    ma = mean(a)
    mb = mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    return cov / (va * vb) ** 0.5


def _changes(values: list[float]) -> list[float]:
    return [values[i] - values[i - 1] for i in range(1, len(values))]


def find_lead_lag(
    leader: list[float],
    follower: list[float],
    *,
    max_lag: int = 10,
) -> LeadLagResult:
    if len(leader) != len(follower):
        raise ValueError("series must have equal length")
    if max_lag < 0:
        raise ValueError("max_lag must be non-negative")

    leader_changes = _changes(leader)
    follower_changes = _changes(follower)

    best_lag: int | None = None
    best_corr = -2.0
    best_obs = 0

    for lag in range(max_lag + 1):
        if lag == 0:
            a, b = leader_changes, follower_changes
        else:
            a, b = leader_changes[:-lag], follower_changes[lag:]
        corr = _corr(a, b)
        if corr > best_corr:
            best_corr = corr
            best_lag = lag
            best_obs = len(a)

    return LeadLagResult(best_lag, best_corr, best_obs)
