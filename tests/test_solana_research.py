import pytest

from noema.solana_research import JupiterTrenchResearchClient, SolanaRpcResearchClient


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
    async def _get(self, path):
        if path == "/tokens/v2/recent":
            return [{"id": "mint-a"}]
        if path == "/tokens/v2/toporganicscore/5m":
            return [{"id": "mint-b", "organicScore": 88.0}]
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
