from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

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
        async for market in self._paged(
            "/markets", {"status": "settled", "limit": 1000, "mve_filter": "exclude"}
        ):
            yield market
        async for market in self._paged(
            "/historical/markets", {"limit": 1000, "mve_filter": "exclude"}
        ):
            yield market

    async def settled_page(
        self, partition: str, *, cursor: str | None, limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One bounded, cursor-addressable page from live or historical settlements."""
        if partition not in {"live", "historical"} or not 1 <= limit <= 1000:
            raise ValueError("invalid settlement partition or page limit")
        endpoint = "/markets" if partition == "live" else "/historical/markets"
        params: dict[str, Any] = {"limit": limit, "mve_filter": "exclude"}
        if partition == "live":
            params["status"] = "settled"
        if cursor:
            params["cursor"] = cursor
        response = await self.client.get(endpoint, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("malformed settlement page")
        markets, next_cursor = payload.get("markets"), payload.get("cursor")
        if (not isinstance(markets, list) or any(not isinstance(m, dict) for m in markets)
                or next_cursor is not None and not isinstance(next_cursor, str)):
            raise ValueError("malformed settlement page")
        return markets, next_cursor or None

    async def market_by_ticker(self, ticker: str) -> dict[str, Any] | None:
        """Check a pending forecast across Kalshi's live and archived partitions."""
        if not ticker or not all(char.isalnum() or char == "-" for char in ticker):
            raise ValueError("invalid market ticker")
        for endpoint in (f"/markets/{quote(ticker, safe='')}",
                         f"/historical/markets/{quote(ticker, safe='')}"):
            response = await self.client.get(endpoint)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("market"), dict):
                raise TypeError("malformed individual market")
            market = payload["market"]
            if market.get("ticker") != ticker:
                raise ValueError("individual market ticker mismatch")
            return market
        return None

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
