from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResilienceResult:
    shock_size: float
    recovered_fraction: float
    recovery_steps: int | None
    resilient: bool


def assess_price_resilience(
    *,
    pre_shock: float,
    shocked: float,
    subsequent: list[float],
    recovery_fraction: float = 0.75,
) -> ResilienceResult:
    if not 0 <= recovery_fraction <= 1:
        raise ValueError("recovery_fraction must be in [0, 1]")
    shock_size = shocked - pre_shock
    if shock_size == 0:
        return ResilienceResult(0.0, 1.0, 0, True)

    target = shocked - shock_size * recovery_fraction
    direction = 1 if shock_size > 0 else -1

    best_fraction = 0.0
    recovery_steps: int | None = None
    for index, value in enumerate(subsequent, start=1):
        recovered = (shocked - value) / shock_size
        best_fraction = max(best_fraction, recovered)
        if direction > 0 and value <= target:
            recovery_steps = index
            break
        if direction < 0 and value >= target:
            recovery_steps = index
            break

    return ResilienceResult(
        shock_size=shock_size,
        recovered_fraction=max(0.0, min(best_fraction, 1.0)),
        recovery_steps=recovery_steps,
        resilient=recovery_steps is not None,
    )
