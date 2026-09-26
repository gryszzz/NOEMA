from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .trade_telemetry import decimal_value


@dataclass(frozen=True)
class SeriesFeeChange:
    change_id: str
    series_ticker: str
    fee_type: str
    fee_multiplier: Decimal
    scheduled_at: datetime


@dataclass(frozen=True)
class EventFeeChange:
    change_id: str
    event_ticker: str
    fee_type_override: str | None
    fee_multiplier_override: Decimal | None
    scheduled_at: datetime


def parse_series_fee_change(raw: dict[str, Any]) -> SeriesFeeChange:
    return SeriesFeeChange(
        change_id=str(raw["id"]),
        series_ticker=str(raw["series_ticker"]),
        fee_type=str(raw["fee_type"]),
        fee_multiplier=decimal_value(raw["fee_multiplier"]),
        scheduled_at=datetime.fromisoformat(str(raw["scheduled_ts"])),
    )


def parse_event_fee_change(raw: dict[str, Any]) -> EventFeeChange:
    multiplier = raw.get("fee_multiplier_override")
    return EventFeeChange(
        change_id=str(raw["id"]),
        event_ticker=str(raw["event_ticker"]),
        fee_type_override=raw.get("fee_type_override"),
        fee_multiplier_override=(
            None if multiplier is None else decimal_value(multiplier)
        ),
        scheduled_at=datetime.fromisoformat(str(raw["scheduled_ts"])),
    )
