from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


def decimal_value(value: object | None, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


@dataclass(frozen=True)
class ObservedOrder:
    order_id: str
    ticker: str
    status: str
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
class ObservedFill:
    fill_id: str
    order_id: str
    ticker: str
    outcome_side: str | None
    count: Decimal
    yes_price: Decimal | None
    no_price: Decimal | None
    is_taker: bool
    fee_cost: Decimal
    created_at: datetime | None


@dataclass(frozen=True)
class ObservedPosition:
    ticker: str
    position: Decimal
    total_traded: Decimal
    exposure: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal
    updated_at: datetime | None


@dataclass(frozen=True)
class QueueObservation:
    order_id: str
    contracts_ahead: Decimal
