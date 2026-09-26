from datetime import UTC, datetime, timedelta

import pytest

from noema.solana_research import (
    JupiterTrenchResearchClient,
    SolanaRpcResearchClient,
    jupiter_control_state,
    jupiter_first_pool_at,
    jupiter_launch_tick,
)


class FakeSolanaClient(SolanaRpcResearchClient):
    async def _rpc(self, method, params):
        if method == "getTokenSupply":
            return {
                "value": {
                    "amount": "1000",
                    "decimals": 2,
                    "uiAmountString": "10.00",
                }
            }
        if method == "getTokenLargestAccounts":
            return {
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
async def test_jupiter_research_feeds_are_read_only_data() -> None:
    client = FakeJupiterClient()
    assert (await client.recent_tradeable_tokens())[0]["id"] == "mint-a"
    assert (await client.top_organic_tokens_5m())[0]["organicScore"] == 88.0


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
