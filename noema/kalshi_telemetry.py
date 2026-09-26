from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from .config import KalshiConfig
from .trade_telemetry import (
    ObservedFill,
    ObservedOrder,
    ObservedPosition,
    QueueObservation,
    decimal_value,
)
from .venues.kalshi import KalshiSigner


def _dt(value: object | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _price(raw: dict[str, Any], name: str) -> Decimal | None:
    value = raw.get(name)
    return None if value is None else decimal_value(value)


class KalshiTelemetry:
    """Read-only authenticated order/fill/position telemetry."""

    def __init__(
        self,
        config: KalshiConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or KalshiConfig.from_env()
        if not self.config.key_id or not self.config.private_key_path:
            raise RuntimeError("Kalshi telemetry requires API credentials")
        self.signer = KalshiSigner(self.config.key_id, self.config.private_key_path)
        self.client = client or httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "NOEMA/0.1"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        retries: int = 4,
    ) -> dict[str, Any]:
        delay = 0.25
        sign_path = f"/trade-api/v2{endpoint}"
        for attempt in range(retries + 1):
            response = await self.client.get(
                endpoint,
                params=params,
                headers=self.signer.headers("GET", sign_path),
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            if attempt == retries:
                response.raise_for_status()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 4.0)
        raise RuntimeError("unreachable")

    async def orders(self) -> list[ObservedOrder]:
        payload = await self._get(
            "/portfolio/orders",
            params={"subaccount": 0, "limit": 1000},
        )
        return [parse_order(raw) for raw in payload.get("orders", [])]

    async def fills(self, *, order_id: str | None = None) -> list[ObservedFill]:
        params: dict[str, Any] = {"subaccount": 0, "limit": 1000}
        if order_id:
            params["order_id"] = order_id
        payload = await self._get("/portfolio/fills", params=params)
        return [parse_fill(raw) for raw in payload.get("fills", [])]

    async def positions(self) -> list[ObservedPosition]:
        payload = await self._get(
            "/portfolio/positions",
            params={"subaccount": 0, "limit": 1000},
        )
        return [parse_position(raw) for raw in payload.get("market_positions", [])]

    async def queue_position(self, order_id: str) -> QueueObservation:
        payload = await self._get(f"/portfolio/orders/{order_id}/queue_position")
        return QueueObservation(
            order_id=order_id,
            contracts_ahead=decimal_value(payload["queue_position_fp"]),
        )

    async def series_fee_changes(self) -> list[dict[str, Any]]:
        payload = await self._get(
            "/series/fee_changes",
            params={"show_historical": True},
        )
        return list(payload.get("series_fee_change_arr", []))

    async def event_fee_changes(self) -> list[dict[str, Any]]:
        payload = await self._get(
            "/events/fee_changes",
            params={"show_historical": True},
        )
        return list(payload.get("event_fee_change_arr", []))


def parse_order(raw: dict[str, Any]) -> ObservedOrder:
    return ObservedOrder(
        order_id=str(raw["order_id"]),
        ticker=str(raw.get("ticker") or raw.get("market_ticker") or ""),
        status=str(raw.get("status") or ""),
        initial_count=decimal_value(raw.get("initial_count_fp")),
        fill_count=decimal_value(raw.get("fill_count_fp")),
        remaining_count=decimal_value(raw.get("remaining_count_fp")),
        yes_price=_price(raw, "yes_price_dollars"),
        no_price=_price(raw, "no_price_dollars"),
        taker_fill_cost=decimal_value(raw.get("taker_fill_cost_dollars")),
        maker_fill_cost=decimal_value(raw.get("maker_fill_cost_dollars")),
        taker_fees=decimal_value(raw.get("taker_fees_dollars")),
        maker_fees=decimal_value(raw.get("maker_fees_dollars")),
        order_group_id=raw.get("order_group_id"),
        created_at=_dt(raw.get("created_time")),
        updated_at=_dt(raw.get("last_update_time")),
    )


def parse_fill(raw: dict[str, Any]) -> ObservedFill:
    return ObservedFill(
        fill_id=str(raw["fill_id"]),
        order_id=str(raw["order_id"]),
        ticker=str(raw.get("ticker") or raw.get("market_ticker") or ""),
        outcome_side=raw.get("outcome_side") or raw.get("side"),
        count=decimal_value(raw.get("count_fp")),
        yes_price=_price(raw, "yes_price_dollars"),
        no_price=_price(raw, "no_price_dollars"),
        is_taker=bool(raw.get("is_taker")),
        fee_cost=decimal_value(raw.get("fee_cost")),
        created_at=_dt(raw.get("created_time")),
    )


def parse_position(raw: dict[str, Any]) -> ObservedPosition:
    return ObservedPosition(
        ticker=str(raw["ticker"]),
        position=decimal_value(raw.get("position_fp")),
        total_traded=decimal_value(raw.get("total_traded_dollars")),
        exposure=decimal_value(raw.get("market_exposure_dollars")),
        realized_pnl=decimal_value(raw.get("realized_pnl_dollars")),
        fees_paid=decimal_value(raw.get("fees_paid_dollars")),
        updated_at=_dt(raw.get("last_updated_ts")),
    )
