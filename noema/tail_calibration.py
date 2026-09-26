from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TailCalibration:
    count: int
    mean_probability: float
    observed_rate: float
    signed_error: float
    absolute_error: float


def evaluate_tail(
    forecasts: list[tuple[float, int]],
    *,
    upper_tail: bool,
    threshold: float = 0.80,
) -> TailCalibration:
    if not 0.5 <= threshold < 1:
        raise ValueError("threshold must be in [0.5, 1)")

    selected: list[tuple[float, int]] = []
    for probability, outcome in forecasts:
        if not 0 <= probability <= 1:
            raise ValueError("probabilities must be in [0, 1]")
        if outcome not in {0, 1}:
            raise ValueError("outcomes must be 0 or 1")
        if upper_tail and probability >= threshold:
            selected.append((probability, outcome))
        if not upper_tail and probability <= 1 - threshold:
            selected.append((probability, outcome))

    if not selected:
        return TailCalibration(0, 0.0, 0.0, 0.0, 0.0)

    count = len(selected)
    mean_probability = sum(probability for probability, _ in selected) / count
    observed_rate = sum(outcome for _, outcome in selected) / count
    signed_error = mean_probability - observed_rate
    return TailCalibration(
        count=count,
        mean_probability=mean_probability,
        observed_rate=observed_rate,
        signed_error=signed_error,
        absolute_error=abs(signed_error),
    )
