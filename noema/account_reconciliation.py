from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .trade_telemetry import ObservedFill, ObservedOrder, ObservedPosition


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    detail: str


@dataclass(frozen=True)
class ReconciliationReport:
    ok: bool
    issues: tuple[ReconciliationIssue, ...]


def reconcile_order_fills(
    orders: list[ObservedOrder],
    fills: list[ObservedFill],
) -> ReconciliationReport:
    issues: list[ReconciliationIssue] = []
    by_order: dict[str, Decimal] = {}
    for fill in fills:
        by_order[fill.order_id] = by_order.get(fill.order_id, Decimal(0)) + fill.count

    order_ids = {order.order_id for order in orders}
    for order in orders:
        observed = by_order.get(order.order_id, Decimal(0))
        if observed != order.fill_count:
            issues.append(
                ReconciliationIssue(
                    "fill_count_mismatch",
                    f"{order.order_id}: order={order.fill_count} fills={observed}",
                )
            )
        if order.fill_count + order.remaining_count > order.initial_count:
            issues.append(
                ReconciliationIssue(
                    "count_overflow",
                    f"{order.order_id}: filled + remaining exceeds initial",
                )
            )

    for fill in fills:
        if fill.order_id not in order_ids:
            issues.append(
                ReconciliationIssue(
                    "orphan_fill",
                    f"{fill.fill_id}: order {fill.order_id} not present",
                )
            )

    return ReconciliationReport(ok=not issues, issues=tuple(issues))


def reconcile_position_fees(
    positions: list[ObservedPosition],
    fills: list[ObservedFill],
) -> ReconciliationReport:
    issues: list[ReconciliationIssue] = []
    fees_by_ticker: dict[str, Decimal] = {}
    for fill in fills:
        fees_by_ticker[fill.ticker] = fees_by_ticker.get(fill.ticker, Decimal(0)) + fill.fee_cost

    for position in positions:
        observed = fees_by_ticker.get(position.ticker, Decimal(0))
        if observed > position.fees_paid:
            issues.append(
                ReconciliationIssue(
                    "fill_fees_exceed_position_fees",
                    f"{position.ticker}: fills={observed} position={position.fees_paid}",
                )
            )

    return ReconciliationReport(ok=not issues, issues=tuple(issues))
