"""Public Polymarket US market-data adapter using the official SDK.

This adapter is intentionally read-only. Authenticated portfolio and trading
calls are a separate capability and remain unavailable until owner credentials,
account reconciliation, and deterministic execution policy are configured.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_us import PolymarketUS

from noema.models import MarketSnapshot
from noema.venues.base import VenueAdapter


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(number) if number.is_finite() else None


def _price(value: Any) -> float | None:
    if isinstance(value, dict):
        return _number(value.get("value"))
    return _number(value)


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class PolymarketUSVenue(VenueAdapter):
    """Polymarket US public market data; never submits orders."""

    name = "polymarket-us"
    supports_live_execution = False

    def __init__(self, client: Any | None = None, *, timeout: float = 7.0) -> None:
        self.client = client or PolymarketUS(timeout=timeout, max_retries=0)

    def close(self) -> None:
        self.client.close()

    async def market_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        active: bool = True,
    ) -> tuple[list[MarketSnapshot], str | None]:
        try:
            offset = int(cursor) if cursor else 0
        except (TypeError, ValueError):
            raise ValueError("invalid Polymarket US page cursor") from None
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Polymarket US page requires offset >= 0 and limit 1..100")
        payload = await asyncio.to_thread(
            self.client.markets.list,
            {"limit": limit, "offset": offset, "active": active, "closed": False},
        )
        raw_markets = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(raw_markets, list):
            raise TypeError("Polymarket US market response is malformed")
        snapshots: list[MarketSnapshot] = []
        for market in raw_markets:
            if not isinstance(market, dict):
                raise TypeError("Polymarket US market response contains malformed data")
            if not market.get("slug") or market.get("closed") is True:
                continue
            try:
                bbo_payload = await asyncio.to_thread(self.client.markets.bbo, str(market["slug"]))
            except Exception as exc:
                # Missing or rate-limited quotes stay unavailable; do not invent a price.
                if type(exc).__name__ in {"RateLimitError", "NotFoundError"}:
                    continue
                raise
            bbo = (bbo_payload.get("marketData")
                   if isinstance(bbo_payload, dict) else None)
            if not isinstance(bbo, dict) or bbo.get("marketSlug") != market["slug"]:
                continue
            bid = _price(bbo.get("bestBid"))
            ask = _price(bbo.get("bestAsk"))
            if bid is not None and not 0 <= bid <= 1:
                bid = None
            if ask is not None and not 0 <= ask <= 1:
                ask = None
            if bid is not None and ask is not None and bid > ask:
                bid = ask = None
            # BBO bidDepth/askDepth are not necessarily contract quantities, so
            # no USD liquidity is asserted without verified book levels.
            snapshots.append(MarketSnapshot(
                venue="polymarket-us",
                market_id=str(market["slug"]),
                title=str(market.get("title") or market.get("question") or market["slug"]),
                yes_bid=bid,
                yes_ask=ask,
                no_bid=None if ask is None else max(0.0, 1.0 - ask),
                no_ask=None if bid is None else min(1.0, 1.0 - bid),
                liquidity_usd=None,
                closes_at=_datetime(market.get("endDate")),
                resolution_rules=(
                    str(market.get("description"))
                    if market.get("description") else None
                ),
                captured_at=datetime.now(UTC),
            ))
        next_cursor = str(offset + limit) if len(raw_markets) == limit else None
        return snapshots, next_cursor

    async def markets(self) -> AsyncIterator[MarketSnapshot]:
        cursor: str | None = None
        page_size = 100
        while True:
            page, next_cursor = await self.market_page(cursor=cursor, limit=page_size)
            for market in page:
                yield market
            if not next_cursor:
                return
            cursor = next_cursor

    async def book(self, slug: str) -> dict[str, Any]:
        if not slug or "/" in slug:
            raise ValueError("invalid Polymarket US market slug")
        payload = await asyncio.to_thread(self.client.markets.book, slug)
        data = payload.get("marketData") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("marketSlug") != slug:
            raise ValueError("Polymarket US book response does not match requested market")
        return data

    async def settlement(self, slug: str) -> dict[str, Any]:
        if not slug or "/" in slug:
            raise ValueError("invalid Polymarket US market slug")
        payload = await asyncio.to_thread(self.client.markets.settlement, slug)
        return payload if isinstance(payload, dict) else {}
