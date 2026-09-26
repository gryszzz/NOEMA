from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PostFillMove:
    fill_price: Decimal
    later_mark: Decimal
    horizon_seconds: int
    move: Decimal


def measure_post_fill_move(
    *,
    fill_price: Decimal,
    later_mark: Decimal,
    horizon_seconds: int,
) -> PostFillMove:
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    if not Decimal(0) <= fill_price <= Decimal(1):
        raise ValueError("fill_price must be in [0, 1]")
    if not Decimal(0) <= later_mark <= Decimal(1):
        raise ValueError("later_mark must be in [0, 1]")
    return PostFillMove(
        fill_price=fill_price,
        later_mark=later_mark,
        horizon_seconds=horizon_seconds,
        move=later_mark - fill_price,
    )
