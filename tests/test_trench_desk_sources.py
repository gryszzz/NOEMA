import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from noema.solana_research import (
    GeckoTerminalPoolClient,
    ProviderFailure,
    PumpFunLaunchClient,
    SlidingWindowRateLimiter,
    XChatterSource,
    parse_gecko_pool,
)

POOL = {
    "attributes": {
        "address": "POOL1", "name": "WEFT / SOL", "base_token_price_usd": "0.0012",
        "reserve_in_usd": "45000.5", "fdv_usd": "1200000",
        "pool_created_at": "2026-10-21T10:00:00Z",
        "price_change_percentage": {"m5": "2.5", "h1": "-12"},
        "transactions": {"m5": {"buys": 4, "sells": 3}, "h1": {"buys": 40, "sells": 31}},
        "volume_usd": {"h1": "8000"},
    },
    "relationships": {"base_token": {"data": {"id": "solana_MINT1"}}},
}


def test_parse_gecko_pool_fields():
    pool = parse_gecko_pool(POOL)
    assert pool is not None
    assert pool.mint == "MINT1" and pool.symbol == "WEFT"
    assert pool.liquidity_usd == pytest.approx(45000.5)
    assert pool.price_change_m5 == 2.5 and pool.price_change_h1 == -12
    assert pool.trades_m5 == 7 and pool.trades_h1 == 71
    assert pool.pool_created_at == datetime(2026, 10, 21, 10, tzinfo=UTC)


def test_parse_gecko_pool_rejects_bad_price():
    bad = {**POOL, "attributes": {**POOL["attributes"], "base_token_price_usd": "0"}}
    assert parse_gecko_pool(bad) is None


def test_rate_limiter_waits_when_window_full():
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = SlidingWindowRateLimiter(2, 60.0, clock=lambda: now[0], sleep=fake_sleep)

    async def run():
        for _ in range(3):
            await limiter.acquire()

    asyncio.run(run())
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(60.01)


def test_gecko_client_uses_limiter_and_classifies_errors():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "trending" in request.url.path:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"data": [POOL]})

    client = GeckoTerminalPoolClient(transport=httpx.MockTransport(handler))
    pools = asyncio.run(client.new_pools())
    assert [p.mint for p in pools] == ["MINT1"]
    with pytest.raises(ProviderFailure) as info:
        asyncio.run(client.trending_pools())
    assert info.value.provider == "geckoterminal"
    assert len(client.limiter._calls) == 2


def test_pumpfun_newest_parses_public_listing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"mint": "M", "symbol": "X", "usd_market_cap": 5000}])

    client = PumpFunLaunchClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(client.newest(5))[0]["mint"] == "M"


def test_chatter_is_research_only_stub():
    result = asyncio.run(XChatterSource(enabled=True).chatter("WEFT", "MINT1"))
    assert result["status"] == "unavailable" and result["mentions"] is None
