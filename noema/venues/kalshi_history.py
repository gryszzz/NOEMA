from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from noema.config import KalshiConfig


class KalshiHistory:
    """Reads resolved Kalshi markets from live + historical partitions."""

    def __init__(
        self,
        config: KalshiConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or KalshiConfig.from_env()
        self.client = client or httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "NOEMA/0.1"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def cutoff(self) -> dict[str, Any]:
        response = await self.client.get("/historical/cutoff")
        response.raise_for_status()
        return response.json()

    async def settled_markets(self) -> AsyncIterator[dict[str, Any]]:
        async for market in self._paged("/markets", {"status": "settled", "limit": 1000}):
            yield market
        async for market in self._paged("/historical/markets", {"limit": 1000}):
            yield market

    async def _paged(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            response = await self.client.get(endpoint, params=query)
            response.raise_for_status()
            payload = response.json()
            for market in payload.get("markets", []):
                yield market
            cursor = payload.get("cursor") or None
            if not cursor:
                break


def resolved_outcome(raw: dict[str, Any]) -> int | None:
    result = str(raw.get("result") or "").lower()
    if result == "yes":
        return 1
    if result == "no":
        return 0
    return None
