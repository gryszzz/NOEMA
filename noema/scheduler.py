from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def run_forever(
    scan: Callable[[], Awaitable[object]],
    *,
    interval_seconds: float = 60.0,
) -> None:
    """Run one bounded scan at a fixed cadence.

    The scan itself owns market filtering, evidence collection, forecasting,
    risk decisions, and persistence. Exceptions are not swallowed: an unhealthy
    autonomous process should stop and surface the failure rather than trade blind.
    """
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be >= 1")

    while True:
        await scan()
        await asyncio.sleep(interval_seconds)
