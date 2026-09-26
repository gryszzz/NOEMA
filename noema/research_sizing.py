from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchSizing:
    full_kelly_fraction: float
    fractional_kelly_fraction: float
    capped_fraction: float


def binary_kelly_fraction(
    *,
    probability: float,
    price: float,
    fraction: float = 0.25,
    hard_cap: float = 0.01,
) -> ResearchSizing:
    """Research-only sizing diagnostic for a $1-settled binary contract.

    This function is intentionally not wired to execution. It helps evaluate how
    sensitive sizing would be to estimated edge under strict fractional/capped use.
    """
    if not 0 < price < 1:
        raise ValueError("price must be in (0, 1)")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be in [0, 1]")
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must be in [0, 1]")
    if not 0 <= hard_cap <= 1:
        raise ValueError("hard_cap must be in [0, 1]")

    b = (1 - price) / price
    q = 1 - probability
    full = (b * probability - q) / b
    full = max(0.0, full)
    fractional = full * fraction
    return ResearchSizing(
        full_kelly_fraction=full,
        fractional_kelly_fraction=fractional,
        capped_fraction=min(fractional, hard_cap),
    )
