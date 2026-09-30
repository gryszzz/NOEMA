import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from noema.solana_research import (
    DexScreenerTrenchPriceClient,
    JupiterTrenchResearchClient,
    ProviderFailure,
    SolanaRpcResearchClient,
    jupiter_control_state,
    jupiter_first_pool_at,
    jupiter_launch_tick,
)


class FakeSolanaClient(SolanaRpcResearchClient):
    async def _rpc(self, method, params, *, endpoint=None):
        if method == "getSlot":
            return 100
        if method == "getTokenSupply":
            return {
                "context": {"slot": 100},
                "value": {
                    "amount": "1000",
                    "decimals": 2,
                    "uiAmountString": "10.00",
                }
            }
        if method == "getTokenLargestAccounts":
            return {
                "context": {"slot": 101},
                "value": [
                    {"address": "a", "amount": "200", "decimals": 2},
                    {"address": "b", "amount": "100", "decimals": 2},
                ]
            }
        raise AssertionError(method)


class FakeJupiterClient(JupiterTrenchResearchClient):
    async def _get(self, path, *, params=None):
        if path == "/tokens/v2/recent":
            return [{"id": "mint-a"}]
        if path == "/tokens/v2/toporganicscore/5m":
            return [{"id": "mint-b", "organicScore": 88.0}]
        if path == "/tokens/v2/search":
            assert params == {"query": "mint-a,mint-b"}
            return [{"id": "mint-a"}, {"id": "mint-b"}]
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_solana_holder_shares_use_total_supply() -> None:
    shares = await FakeSolanaClient().top_account_supply_shares("mint")
    assert shares == (0.2, 0.1)


@pytest.mark.asyncio
async def test_solana_rpc_retries_rate_limit_and_falls_back_on_stale_context(monkeypatch) -> None:
    import noema.solana_research as module

    calls = {"primary": 0, "fallback": 0}
    sleeps = []

    def handler(request):
        body = json.loads(request.content)
        provider = "primary" if request.url.host == "primary.test" else "fallback"
        calls[provider] += 1
        if provider == "primary" and calls[provider] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        method = body["method"]
        if method == "getSlot":
            result = 100 if provider == "primary" else 202
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        if provider == "primary" and method == "getTokenLargestAccounts":
            result = {"context": {"slot": 99}, "value": [
                {"address": "a", "amount": "20", "decimals": 2},
            ]}
        elif method == "getTokenSupply":
            slot = 100 if provider == "primary" else 200
            result = {"context": {"slot": slot}, "value": {
                "amount": "100", "decimals": 2, "uiAmountString": "1.00",
            }}
        else:
            result = {"context": {"slot": 201}, "value": [
                {"address": "a", "amount": "20", "decimals": 2},
                {"address": "b", "amount": "10", "decimals": 2},
            ]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    client = SolanaRpcResearchClient(
        rpc_url="https://primary.test/rpc", fallback_rpc_url="https://fallback.test/rpc",
        retry_attempts=2,
    )
    shares = await client.top_account_supply_shares("mint")

    assert shares == (0.2, 0.1)
    assert client.last_provider == "fallback"
    assert client.last_context_slot == 201
    assert calls == {"primary": 4, "fallback": 3}
    assert sleeps == [0.0]
    assert client.last_attempt_provenance == {
        "source_alias": "fallback",
        "fallback_used": True,
        "context_slot": 201,
        "observed_slot": 202,
        "freshness_slots": 1,
        "elapsed_ms": client.last_attempt_provenance["elapsed_ms"],
        "failed_providers": [{
            "source_alias": "primary", "operation": "getTokenLargestAccounts",
            "error_class": "stale_or_invalid_context", "http_status": None,
            "attempts": None, "elapsed_ms": None,
        }],
    }


@pytest.mark.asyncio
async def test_solana_failure_retains_safe_http_classification_and_attempt_count(monkeypatch) -> None:
    import noema.solana_research as module

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(429))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    client = SolanaRpcResearchClient(retry_attempts=2)
    with pytest.raises(ProviderFailure) as captured:
        await client._rpc("getSlot", [{"commitment": "confirmed"}])

    assert captured.value.error_class == "rate_limited"
    assert captured.value.http_status == 429
    assert captured.value.attempts == 2
    assert "429" not in str(captured.value)


@pytest.mark.asyncio
async def test_jupiter_retries_rate_limit_without_returning_missing_data(monkeypatch) -> None:
    import noema.solana_research as module

    calls = 0
    real_client = httpx.AsyncClient

    def _increment():
        nonlocal calls
        calls += 1
        return calls

    transport = httpx.MockTransport(lambda request: (
        httpx.Response(429, headers={"Retry-After": "0"}) if _increment() == 1
        else httpx.Response(200, json=[{"id": "mint-a"}])
    ))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    result = await JupiterTrenchResearchClient(retry_attempts=2).recent_tradeable_tokens()
    assert result == [{"id": "mint-a"}]
    assert calls == 2


@pytest.mark.asyncio
async def test_jupiter_research_feeds_are_read_only_data() -> None:
    client = FakeJupiterClient()
    assert (await client.recent_tradeable_tokens())[0]["id"] == "mint-a"
    assert (await client.top_organic_tokens_5m())[0]["organicScore"] == 88.0


@pytest.mark.asyncio
async def test_dexscreener_fallback_requires_solana_base_identity_and_recent_activity(monkeypatch):
    import noema.solana_research as module

    rows = [
        {"chainId": "ethereum", "baseToken": {"address": "mint-a"},
         "priceUsd": "1", "liquidity": {"usd": 1000},
         "txns": {"m5": {"buys": 2}}},
        {"chainId": "solana", "baseToken": {"address": "mint-a"},
         "priceUsd": "9", "liquidity": {"usd": 9000},
         "txns": {"m5": {"buys": 0, "sells": 0}}},
        {"chainId": "solana", "baseToken": {"address": "mint-a"},
         "priceUsd": "0.25", "liquidity": {"usd": 5000},
         "txns": {"m5": {"buys": 2, "sells": 1}}, "pairAddress": "active-pool"},
    ]
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=rows))
    monkeypatch.setattr(module.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=transport, **kw))
    result = await DexScreenerTrenchPriceClient().tokens_by_mint(["mint-a"])
    assert result["mint-a"]["pairAddress"] == "active-pool"


@pytest.mark.asyncio
async def test_jupiter_batch_search_uses_comma_separated_mints() -> None:
    client = FakeJupiterClient()
    result = await client.tokens_by_mint(["mint-a", "mint-b"])
    assert set(result) == {"mint-a", "mint-b"}


def test_jupiter_normalization_preserves_audit_semantics() -> None:
    now = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    token = {
        "id": "mint-a",
        "tokenProgram": "Token2022",
        "usdPrice": 0.002,
        "liquidity": 12000,
        "organicScore": 74,
        "firstPool": {"createdAt": (now - timedelta(minutes=5)).isoformat()},
        "stats24h": {
            "buyVolume": 5000,
            "sellVolume": 1200,
            "buyOrganicVolume": 2100,
            "sellOrganicVolume": 700,
            "numOrganicBuyers": 41,
            "numTraders": 80,
            "numNetBuyers": 25,
        },
        "audit": {
            "mintAuthorityDisabled": True,
            "freezeAuthorityDisabled": False,
            "devBalancePercentage": 4.0,
            "isSus": True,
        },
    }

    assert jupiter_first_pool_at(token) == now - timedelta(minutes=5)
    control = jupiter_control_state(token)
    assert control.mint_authority_present is False
    assert control.freeze_authority_present is True
    assert control.suspicious_flag is True
    assert control.transfer_fee_bps is None

    tick = jupiter_launch_tick(token, observed_at=now, holder_shares=(0.10, 0.05))
    assert tick.organic_net_buyers == 41
    assert tick.total_traders == 80
    assert tick.creator_supply_fraction == pytest.approx(0.04)
    assert tick.holder_shares == (0.10, 0.05)


def test_missing_optional_flow_metrics_remain_unknown():
    first_pool = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)
    tick = jupiter_launch_tick({
        "id": "mint-a", "firstPool": {"createdAt": first_pool.isoformat()},
        "usdPrice": 0.01, "liquidity": 1000,
    }, observed_at=first_pool + timedelta(seconds=30))
    assert tick.buy_volume_usd is None
    assert tick.sell_volume_usd is None
