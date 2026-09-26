from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .account_reconciliation import reconcile_order_fills, reconcile_position_fees
from .fill_accounting import summarize_fills
from .trade_telemetry import ObservedFill, ObservedOrder, ObservedPosition


def build_telemetry_report(
    *,
    orders: list[ObservedOrder],
    fills: list[ObservedFill],
    positions: list[ObservedPosition],
) -> dict[str, Any]:
    by_order = {order.order_id: order for order in orders}
    execution_rows = []
    for order_id, order in by_order.items():
        execution_rows.append(
            asdict(
                summarize_fills(
                    order,
                    [fill for fill in fills if fill.order_id == order_id],
                )
            )
        )

    order_check = reconcile_order_fills(orders, fills)
    fee_check = reconcile_position_fees(positions, fills)

    return {
        "orders": len(orders),
        "fills": len(fills),
        "positions": len(positions),
        "realized_pnl": sum((position.realized_pnl for position in positions), start=0),
        "fees_paid": sum((position.fees_paid for position in positions), start=0),
        "open_exposure": sum((position.exposure for position in positions), start=0),
        "execution": execution_rows,
        "order_fill_reconciliation": {
            "ok": order_check.ok,
            "issues": [asdict(issue) for issue in order_check.issues],
        },
        "position_fee_reconciliation": {
            "ok": fee_check.ok,
            "issues": [asdict(issue) for issue in fee_check.issues],
        },
    }
