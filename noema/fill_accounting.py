from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .trade_telemetry import ObservedFill, ObservedOrder


@dataclass(frozen=True)
class FillAccounting:
    order_id: str
    observed_filled_count: Decimal
    order_reported_filled_count: Decimal
    remaining_count: Decimal
    gross_fill_cost: Decimal
    fees_paid: Decimal
    average_fill_price: Decimal | None
    maker_fraction: Decimal | None


def summarize_fills(
    order: ObservedOrder,
    fills: list[ObservedFill],
) -> FillAccounting:
    matching = [fill for fill in fills if fill.order_id == order.order_id]
    filled = sum((fill.count for fill in matching), Decimal(0))
    fees = sum((fill.fee_cost for fill in matching), Decimal(0))

    gross = Decimal(0)
    maker_count = Decimal(0)
    for fill in matching:
        price = fill.yes_price if fill.outcome_side == "yes" else fill.no_price
        if price is not None:
            gross += price * fill.count
        if not fill.is_taker:
            maker_count += fill.count

    return FillAccounting(
        order_id=order.order_id,
        observed_filled_count=filled,
        order_reported_filled_count=order.fill_count,
        remaining_count=order.remaining_count,
        gross_fill_cost=gross,
        fees_paid=fees,
        average_fill_price=(gross / filled if filled > 0 else None),
        maker_fraction=(maker_count / filled if filled > 0 else None),
    )
