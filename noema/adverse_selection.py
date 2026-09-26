from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AdverseSelection:
    fill_price: Decimal
    mark_price: Decimal
    horizon_seconds: int
    signed_move: Decimal
    favorable: bool


def measure_adverse_selection(
    *,
    outcome_side: str,
    fill_price: Decimal,
    mark_price: Decimal,
    horizon_seconds: int,
) -> AdverseSelection:
    if outcome_side not in {"yes", "no"}:
        raise ValueError("outcome_side must be yes or no")
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")

    # A higher subsequent price is favorable for the side that was bought.
    signed_move = mark_price - fill_price
    return AdverseSelection(
        fill_price=fill_price,
        mark_price=mark_price,
        horizon_seconds=horizon_seconds,
        signed_move=signed_move,
        favorable=signed_move >= 0,
    )
