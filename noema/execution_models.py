from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


def D(value: object | None, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


@dataclass(frozen=True)
class OrderState:
    order_id: str
    client_order_id: str | None
    ticker: str
    status: str
    outcome_side: str | None
    book_side: str | None
    initial_count: Decimal
    fill_count: Decimal
    remaining_count: Decimal
    yes_price: Decimal | None
    no_price: Decimal | None
    taker_fill_cost: Decimal
    maker_fill_cost: Decimal
    taker_fees: Decimal
    maker_fees: Decimal
    order_group_id: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class FillState:
    fill_id: str
    order_id: str
    ticker: str
    outcome_side: str | None
    book_side: str | None
    count: Decimal
    yes_price: Decimal | None
    no_price: Decimal | None
    is_taker: bool
    fee_cost: Decimal
    created_at: datetime | None


@dataclass(frozen=True)
class PositionState:
    ticker: str
    position: Decimal
    total_traded: Decimal
    exposure: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    updated_at: datetime | None


@dataclass(frozen=True)
class QueueState:
    order_id: str
    contracts_ahead: Decimal


@dataclass(frozen=True)
class ExecutionEconomics:
    order_id: str
    filled_count: Decimal
    remaining_count: Decimal
    gross_fill_cost: Decimal
    fees_paid: Decimal
    average_cost_per_contract: Decimal | None
    fee_per_contract: Decimal | None
    maker_fraction: Decimal | None


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    message: str
