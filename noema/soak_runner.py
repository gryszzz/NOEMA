from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from .models import MarketSnapshot
from .soak import SoakStore
from .venues.base import VenueAdapter


@dataclass(frozen=True)
class SoakRunResult:
    scanned: int
    valid: int
    invalid: int


class PagedVenue(Protocol):
    name: str

    async def market_page(
        self, *, cursor: str | None, limit: int,
    ) -> tuple[list[MarketSnapshot], str | None]: ...


async def collect_rotating_market_batch(
    venue: PagedVenue,
    store: SoakStore,
    *,
    max_markets: int,
    on_valid: Callable[[MarketSnapshot], None] | None = None,
) -> SoakRunResult:
    """Scan successive API pages across cycles, restarting after the final page."""
    if not 1 <= max_markets <= 1000:
        raise ValueError("max_markets must be between 1 and 1000")
    source = f"{venue.name}:markets"
    cursor = store.scan_cursor(source)
    try:
        try:
            markets, next_cursor = await venue.market_page(
                cursor=cursor, limit=max_markets,
            )
        except httpx.HTTPStatusError as exc:
            if not cursor or exc.response.status_code != 400:
                raise
            # A saved cursor may expire while the worker is offline. Restart
            # only on the API's invalid-request response, never on a 429/5xx.
            store.set_scan_cursor(source, None)
            markets, next_cursor = await venue.market_page(
                cursor=None, limit=max_markets,
            )
        valid = 0
        for market in markets:
            if store.append_market(market).valid:
                valid += 1
                if on_valid is not None:
                    on_valid(market)
        # A failed request or failed write must not silently skip a page.
        store.set_scan_cursor(source, next_cursor)
        store.heartbeat(source, ok=True, detail=(
            f"scanned={len(markets)};valid={valid};invalid={len(markets) - valid};"
            f"next_page={bool(next_cursor)}"
        ))
        return SoakRunResult(len(markets), valid, len(markets) - valid)
    except Exception as exc:
        store.heartbeat(source, ok=False, detail=type(exc).__name__)
        raise


async def collect_market_snapshot_batch(
    venue: VenueAdapter,
    store: SoakStore,
    *,
    max_markets: int | None = None,
    on_valid: Callable[[MarketSnapshot], None] | None = None,
) -> SoakRunResult:
    scanned = 0
    valid = 0
    invalid = 0

    try:
        async for market in venue.markets():
            scanned += 1
            validation = store.append_market(market)
            if validation.valid:
                valid += 1
                if on_valid is not None:
                    on_valid(market)
            else:
                invalid += 1

            if max_markets is not None and scanned >= max_markets:
                break
        store.heartbeat(
            f"{venue.name}:markets",
            ok=True,
            detail=f"scanned={scanned};valid={valid};invalid={invalid}",
        )
    except Exception as exc:
        store.heartbeat(f"{venue.name}:markets", ok=False, detail=type(exc).__name__)
        raise

    return SoakRunResult(scanned=scanned, valid=valid, invalid=invalid)


async def run_soak_loop(
    venue: VenueAdapter,
    store: SoakStore,
    *,
    interval_seconds: float = 60.0,
    max_markets: int | None = None,
) -> None:
    if interval_seconds < 5:
        raise ValueError("interval_seconds must be >= 5")

    while True:
        await collect_market_snapshot_batch(
            venue,
            store,
            max_markets=max_markets,
        )
        await asyncio.sleep(interval_seconds)
