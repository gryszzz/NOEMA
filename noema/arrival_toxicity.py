from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class TradeArrival:
    side: str
    seconds_from_start: float


@dataclass(frozen=True)
class ArrivalToxicity:
    buy_fraction: float
    longest_directional_run: int
    mean_interarrival_seconds: float | None
    interarrival_cv: float | None
    persistence_score: float
    burstiness_score: float
    classification: str


def _longest_run(sides: list[str]) -> int:
    longest = 0
    current = 0
    previous: str | None = None
    for side in sides:
        if side == previous:
            current += 1
        else:
            previous = side
            current = 1
        longest = max(longest, current)
    return longest


def assess_arrival_toxicity(trades: list[TradeArrival]) -> ArrivalToxicity:
    if not trades:
        return ArrivalToxicity(0.0, 0, None, None, 0.0, 0.0, "low")

    ordered = sorted(trades, key=lambda trade: trade.seconds_from_start)
    for trade in ordered:
        if trade.side not in {"buy", "sell"}:
            raise ValueError("trade side must be buy or sell")
        if trade.seconds_from_start < 0:
            raise ValueError("arrival times must be non-negative")

    sides = [trade.side for trade in ordered]
    buys = sum(side == "buy" for side in sides)
    buy_fraction = buys / len(sides)
    longest = _longest_run(sides)
    persistence = min(longest / max(len(sides), 1), 1.0)

    intervals = [
        ordered[index].seconds_from_start - ordered[index - 1].seconds_from_start
        for index in range(1, len(ordered))
    ]
    mean_interval: float | None = None
    cv: float | None = None
    burstiness = 0.0
    if intervals:
        mean_interval = mean(intervals)
        if mean_interval > 0:
            variance = mean((value - mean_interval) ** 2 for value in intervals)
            cv = variance**0.5 / mean_interval
            burstiness = min(cv / 2.0, 1.0)

    directional_imbalance = abs(buy_fraction - 0.5) * 2
    score = min(
        1.0,
        0.45 * directional_imbalance
        + 0.35 * persistence
        + 0.20 * burstiness,
    )
    if score >= 0.75:
        classification = "high"
    elif score >= 0.50:
        classification = "elevated"
    elif score >= 0.25:
        classification = "moderate"
    else:
        classification = "low"

    return ArrivalToxicity(
        buy_fraction=buy_fraction,
        longest_directional_run=longest,
        mean_interarrival_seconds=mean_interval,
        interarrival_cv=cv,
        persistence_score=persistence,
        burstiness_score=burstiness,
        classification=classification,
    )
