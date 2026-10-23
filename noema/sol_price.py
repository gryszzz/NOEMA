"""Fresh SOL/USD for paper desks. No constant fallback: returns None when nothing is fresh.

Order: DexScreener wSOL (existing runtime source) -> Coinbase spot -> Jupiter price ->
``NOEMA_SOL_USD_FILE`` (default /workspace/marketdata/noema/sol_usd/latest.json, used only if
its ``asof_utc`` is < 2 min old). A good price is cached ~60s and may be reused for up to
``MAX_AGE_S``; beyond that callers must skip.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

WSOL = "So11111111111111111111111111111111111111112"
DEFAULT_FILE = "/workspace/marketdata/noema/sol_usd/latest.json"
CACHE_S = 60.0
MAX_AGE_S = 120.0
FILE_MAX_AGE_S = 120.0
HEARTBEAT = "feed:sol_usd"


def _sane(p: Any) -> float | None:
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    return v if 10 < v < 10_000 else None


def read_file_price(path: str | None = None, now: float | None = None,
                    max_age_s: float = FILE_MAX_AGE_S) -> tuple[float, float] | None:
    path = path or os.getenv("NOEMA_SOL_USD_FILE", DEFAULT_FILE)
    now = now or time.time()
    try:
        with open(path) as f:
            d = json.load(f)
        price = _sane(d.get("price"))
        asof = datetime.fromisoformat(str(d.get("asof_utc") or (d.get("coinbase") or {}).get("asof_utc"))).timestamp()
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if price is None or not (0 <= now - asof < max_age_s):
        return None
    return price, asof


async def _coinbase(client: httpx.AsyncClient) -> float | None:
    r = await client.get("https://api.coinbase.com/v2/prices/SOL-USD/spot")
    r.raise_for_status()
    return _sane(r.json()["data"]["amount"])


async def _jupiter(client: httpx.AsyncClient) -> float | None:
    r = await client.get("https://lite-api.jup.ag/price/v3", params={"ids": WSOL})
    r.raise_for_status()
    return _sane((r.json().get(WSOL) or {}).get("usdPrice"))


class SolPrice:
    def __init__(self, dex: Any = None, client: httpx.AsyncClient | None = None,
                 sources: list[tuple[str, Callable[[], Awaitable[float | None]]]] | None = None,
                 clock: Callable[[], float] = time.time, beat: Callable[..., None] | None = None) -> None:
        self.clock = clock
        self.price: float | None = None
        self.at = -1e18
        self.source: str | None = None
        self._client = client
        if beat is None:
            from .heartbeats import beat as _hb_beat
            beat = _hb_beat
        self._beat = beat
        if sources is None:
            sources = []
            if dex is not None:
                async def _dex() -> float | None:
                    pair = (await dex.tokens_by_mint([WSOL])).get(WSOL) or {}
                    return _sane(pair.get("priceUsd"))
                sources.append(("dexscreener", _dex))
            sources += [("coinbase", lambda: _coinbase(self._http())), ("jupiter", lambda: _jupiter(self._http()))]
        self.sources = sources

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=8.0)
        return self._client

    async def get(self) -> float | None:
        now = self.clock()
        if now - self.at < CACHE_S:
            return self.price
        errors = []
        for name, fn in self.sources:
            try:
                p = _sane(await fn())
            except Exception as exc:  # noqa: BLE001 - try next source
                errors.append(f"{name}:{type(exc).__name__}")
                continue
            if p is not None:
                self.price, self.at, self.source = p, now, name
                self._beat(HEARTBEAT, True, cadence_s=CACHE_S)
                return p
            errors.append(f"{name}:bad")
        f = read_file_price(now=now)
        if f is not None:
            self.price, self.at, self.source = f[0], f[1], "latest.json"
            self._beat(HEARTBEAT, True, cadence_s=CACHE_S)
            return f[0]
        self._beat(HEARTBEAT, False, " ".join(errors) or "no fresh source", cadence_s=CACHE_S)
        if now - self.at < MAX_AGE_S:
            return self.price
        return None
