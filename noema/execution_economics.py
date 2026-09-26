from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .execution_models import ExecutionEconomics, FillState, OrderState


def summarize_execution(
    order: OrderState,
    fills: list[FillState],
) -> ExecutionEconomics:
    matching = [fill for fill in fills if fill.order_id == order.order_id]
    filled = sum((fill.count for fill in matching), Decimal(0))
    fees = sum((fill.fee_cost for fill in matching), Decimal(0))

    gross_cost = Decimal(0)
    maker_count = Decimal(0)
    for fill in matching:
        price = fill.yes_price if fill.outcome_side == "yes" else fill.no_price
        if price is not None:
            gross_cost += price * fill.count
        if not fill.is_taker:
            maker_count += fill.count

    avg_cost = gross_cost / filled if filled > 0 else None
    fee_per_contract = fees / filled if filled > 0 else None
    maker_fraction = maker_count / filled if filled > 0 else None

    return ExecutionEconomics(
        order_id=order.order_id,
        filled_count=filled,
        remaining_count=order.remaining_count,
        gross_fill_cost=gross_cost,
        fees_paid=fees,
        average_cost_per_contract=avg_cost,
        fee_per_contract=fee_per_contract,
        maker_fraction=maker_fraction,
    )


def group_fills_by_order(fills: list[FillState]) -> dict[str, list[FillState]]:
    grouped: dict[str, list[FillState]] = defaultdict(list)
    for fill in fills:
        grouped[fill.order_id].append(fill)
    return dict(grouped)
