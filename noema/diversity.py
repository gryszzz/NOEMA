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

    positive = [signal for signal in signals if signal.weight > 0]
    if not positive:
        return {}

    families: dict[str, list[WeightedSignal]] = defaultdict(list)
    for signal in positive:
        families[signal.family].append(signal)

    family_count = len(families)
    if max_family_fraction * family_count < 1 - 1e-12:
        raise ValueError("family cap is infeasible for the number of families")

    family_raw = {
        family: sum(signal.weight for signal in members)
        for family, members in families.items()
    }

    remaining = set(family_raw)
    allocation: dict[str, float] = {}
    remaining_mass = 1.0

    while remaining:
        base_total = sum(family_raw[family] for family in remaining)
        if base_total <= 0:
            break

        proposed = {
            family: remaining_mass * family_raw[family] / base_total
            for family in remaining
        }
        capped = [
            family
            for family, share in proposed.items()
            if share > max_family_fraction + 1e-12
        ]

        if not capped:
            allocation.update(proposed)
            remaining_mass = 0.0
            break

        for family in capped:
            allocation[family] = max_family_fraction
            remaining_mass -= max_family_fraction
            remaining.remove(family)

    if remaining_mass > 1e-9:
        raise ValueError("unable to allocate family weights under concentration cap")

    result: dict[str, float] = {}
    for family, members in families.items():
        family_share = allocation[family]
        family_total = family_raw[family]
        for signal in members:
            result[signal.name] = family_share * signal.weight / family_total

    normalized = sum(result.values())
    if normalized <= 0:
        return {}
    return {name: weight / normalized for name, weight in result.items()}
