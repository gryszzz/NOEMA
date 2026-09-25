from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import KalshiConfig
from .venues.kalshi import KalshiSigner


@dataclass(frozen=True)
class AccountSnapshot:
    balance: dict[str, Any]
    positions: dict[str, Any]
    orders: dict[str, Any]
    fills: dict[str, Any]
    limits: dict[str, Any]
    user_data_as_of: datetime | None


class KalshiAccount:
    """Authenticated account mirror for the primary Kalshi account."""

    def __init__(
        self,
        config: KalshiConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or KalshiConfig.from_env()
        if not self.config.key_id or not self.config.private_key_path:
            raise RuntimeError("Kalshi account access requires API credentials")
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
        sign_path = f"/trade-api/v2{endpoint}"
        delay = 0.25
        for attempt in range(retries + 1):
            headers = self.signer.headers("GET", sign_path)
            response = await self.client.get(endpoint, params=params, headers=headers)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            if attempt == retries:
                response.raise_for_status()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 4.0)
        raise RuntimeError("unreachable")

    async def balance(self) -> dict[str, Any]:
        return await self._get("/portfolio/balance", params={"subaccount": 0})

    async def positions(self) -> dict[str, Any]:
        return await self._get("/portfolio/positions", params={"subaccount": 0})

    async def orders(self) -> dict[str, Any]:
        return await self._get("/portfolio/orders", params={"subaccount": 0})

    async def fills(self) -> dict[str, Any]:
        return await self._get("/portfolio/fills", params={"subaccount": 0})

    async def limits(self) -> dict[str, Any]:
        return await self._get("/account/limits")

    async def user_data_timestamp(self) -> datetime | None:
        response = await self.client.get("/exchange/user_data_timestamp")
        response.raise_for_status()
        raw = response.json().get("as_of_time")
        if not raw:
            return None
        value = datetime.fromisoformat(str(raw))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def snapshot(self) -> AccountSnapshot:
        balance, positions, orders, fills, limits, as_of = await asyncio.gather(
            self.balance(),
            self.positions(),
            self.orders(),
            self.fills(),
            self.limits(),
            self.user_data_timestamp(),
        )
        return AccountSnapshot(
            balance=balance,
            positions=positions,
            orders=orders,
            fills=fills,
            limits=limits,
            user_data_as_of=as_of,
        )
