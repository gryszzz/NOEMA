from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class WeightedSignal:
    name: str
    family: str
    weight: float


def cap_family_concentration(
    signals: list[WeightedSignal],
    *,
    max_family_fraction: float = 0.50,
) -> dict[str, float]:
    if not 0 < max_family_fraction <= 1:
        raise ValueError("max_family_fraction must be in (0, 1]")

    positive = [s for s in signals if s.weight > 0]
    if not positive:
        return {}

    weights = {s.name: s.weight for s in positive}
    families: dict[str, list[WeightedSignal]] = defaultdict(list)
    for signal in positive:
        families[signal.family].append(signal)

    total = sum(weights.values())
    cap = total * max_family_fraction

    for family_signals in families.values():
        family_total = sum(weights[s.name] for s in family_signals)
        if family_total <= cap or family_total <= 0:
            continue
        scale = cap / family_total
        for signal in family_signals:
            weights[signal.name] *= scale

    normalized_total = sum(weights.values())
    if normalized_total <= 0:
        return {}
    return {name: weight / normalized_total for name, weight in weights.items()}
