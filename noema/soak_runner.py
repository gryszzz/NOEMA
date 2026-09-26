from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .soak import SoakStore
from .venues.base import VenueAdapter


@dataclass(frozen=True)
class SoakRunResult:
    scanned: int
    valid: int
    invalid: int


async def collect_market_snapshot_batch(
    venue: VenueAdapter,
    store: SoakStore,
    *,
    max_markets: int | None = None,
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
