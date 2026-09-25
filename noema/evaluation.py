from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean


_EPSILON = 1e-12


@dataclass(frozen=True)
class ScoredForecast:
    probability_yes: float
    outcome_yes: int
    brier: float
    log_loss: float


def score_forecast(probability_yes: float, outcome_yes: int) -> ScoredForecast:
    if not 0 <= probability_yes <= 1:
        raise ValueError("probability_yes must be in [0, 1]")
    if outcome_yes not in {0, 1}:
        raise ValueError("outcome_yes must be 0 or 1")

    p = min(max(probability_yes, _EPSILON), 1 - _EPSILON)
    brier = (p - outcome_yes) ** 2
    log_loss = -(outcome_yes * math.log(p) + (1 - outcome_yes) * math.log(1 - p))
    return ScoredForecast(
        probability_yes=probability_yes,
        outcome_yes=outcome_yes,
        brier=brier,
        log_loss=log_loss,
    )


@dataclass(frozen=True)
class CalibrationBucket:
    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_rate: float
    absolute_error: float


def calibration_buckets(
    forecasts: list[tuple[float, int]],
    *,
    bucket_width: float = 0.10,
) -> list[CalibrationBucket]:
    if not 0 < bucket_width <= 1:
        raise ValueError("bucket_width must be in (0, 1]")

    bucket_count = math.ceil(1 / bucket_width)
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bucket_count)]

    for probability, outcome in forecasts:
        if not 0 <= probability <= 1:
            raise ValueError("probabilities must be in [0, 1]")
        if outcome not in {0, 1}:
            raise ValueError("outcomes must be 0 or 1")
        index = min(int(probability / bucket_width), bucket_count - 1)
        buckets[index].append((probability, outcome))

    result: list[CalibrationBucket] = []
    for index, rows in enumerate(buckets):
        if not rows:
            continue
        lower = index * bucket_width
        upper = min(1.0, lower + bucket_width)
        avg_probability = mean(p for p, _ in rows)
        observed_rate = mean(float(outcome) for _, outcome in rows)
        result.append(
            CalibrationBucket(
                lower=lower,
                upper=upper,
                count=len(rows),
                mean_probability=avg_probability,
                observed_rate=observed_rate,
                absolute_error=abs(avg_probability - observed_rate),
            )
        )
    return result


def expected_calibration_error(
    forecasts: list[tuple[float, int]],
    *,
    bucket_width: float = 0.10,
) -> float:
    if not forecasts:
        return 0.0
    buckets = calibration_buckets(forecasts, bucket_width=bucket_width)
    total = len(forecasts)
    return sum(bucket.count / total * bucket.absolute_error for bucket in buckets)
