from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from noema.market_discovery import (
    DexScreenerDiscoveryClient,
    MarketDiscoveryConfig,
    MarketDiscoveryError,
    MarketDiscoveryStore,
    collect_and_persist_market_discovery,
    normalize_pair,
)

NOW = datetime(2026, 10, 2, 1, tzinfo=UTC)
EVM_TOKEN = "0x" + "a" * 40
EVM_QUOTE = "0x" + "b" * 40
EVM_PAIR = "0x" + "c" * 40


def pair_row(**updates):
    row = {
        "chainId": "base",
        "dexId": "uniswap",
        "pairAddress": EVM_PAIR,
        "baseToken": {"address": EVM_TOKEN, "name": "Sample Token", "symbol": "SMP"},
        "quoteToken": {"address": EVM_QUOTE, "name": "USD Coin", "symbol": "USDC"},
        "priceUsd": "1.25",
        "priceNative": "0.0005",
        "liquidity": {"usd": 10000},
        "volume": {"h24": 2000, "h6": 400},
        "txns": {"h24": {"buys": 12, "sells": 7}},
        "pairCreatedAt": 1790900000000,
        "transaction": {"data": "must-not-persist"},
    }
    row.update(updates)
    return row


def test_normalization_marks_source_claims_and_disables_authority():
    observation = normalize_pair(
        pair_row(), observed_at=NOW, discovered_chain_id="base",
        discovered_token_address=EVM_TOKEN,
    )
    assert observation is not None
    assert observation.canonical_network_id == "eip155:8453"
    assert observation.base_asset_id == f"eip155:8453/erc20:{EVM_TOKEN}"
    assert observation.pair_id == f"eip155:8453/dex-pair:{EVM_PAIR}"
    assert observation.price_usd == "1.25"
    assert observation.liquidity_usd == "10000"
    assert observation.transactions == {"h24": {"buys": 12, "sells": 7}}
    assert observation.evidence_state == "provider_observed_unverified"
    assert observation.candidate_state == "unreviewed"
    assert observation.execution_authority_state == "disabled"
    assert observation.source_timestamp_semantics.startswith("collector_observed")
    assert "must-not-persist" not in json.dumps(observation.as_dict())


def test_unknown_chain_remains_provider_scoped_without_inferred_identity():
    row = pair_row(chainId="future-chain")
    observation = normalize_pair(
        row, observed_at=NOW, discovered_chain_id="future-chain",
        discovered_token_address=EVM_TOKEN,
    )
    assert observation is not None
    assert observation.canonical_network_id is None
    assert observation.chain_family is None
    assert observation.base_asset_id is None
    assert observation.pair_id == f"dexscreener:future-chain/{EVM_PAIR}"
    assert observation.evidence_state == "provider_observed_unverified"


@pytest.mark.parametrize("row,chain,address", [
    (pair_row(chainId="ethereum"), "base", EVM_TOKEN),
    (pair_row(baseToken={"address": EVM_TOKEN}, quoteToken={"address": EVM_QUOTE}), "base", "0x" + "d" * 40),
    (pair_row(pairAddress="bad-pair"), "base", EVM_TOKEN),
])
def test_discovery_rejects_wrong_identity_or_malformed_pair(row, chain, address):
    result = normalize_pair(
        row, observed_at=NOW, discovered_chain_id=chain,
        discovered_token_address=address,
    )
    assert result is None


def test_malformed_price_remains_unavailable_without_dropping_pair_identity():
    observation = normalize_pair(
        pair_row(priceUsd="NaN"), observed_at=NOW, discovered_chain_id="base",
        discovered_token_address=EVM_TOKEN,
    )
    assert observation is not None
    assert observation.price_usd is None


def test_solana_pair_uses_canonical_network_and_token_address():
    mint = "So11111111111111111111111111111111111111112"
    quote = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    pool = "11111111111111111111111111111111"
    row = {
        "chainId": "solana", "dexId": "raydium", "pairAddress": pool,
        "baseToken": {"address": mint, "symbol": "SOL"},
        "quoteToken": {"address": quote, "symbol": "USDC"},
        "priceUsd": "200", "liquidity": {"usd": 500000},
    }
    observation = normalize_pair(
        row, observed_at=NOW, discovered_chain_id="solana",
        discovered_token_address=mint,
    )
    assert observation is not None
    assert observation.canonical_network_id == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    assert observation.base_asset_id.endswith(f"/token:{mint}")


def test_store_is_idempotent_and_never_promotes_candidate(tmp_path):
    db = str(tmp_path / "discovery.db")
    observation = normalize_pair(
        pair_row(), observed_at=NOW, discovered_chain_id="base",
        discovered_token_address=EVM_TOKEN,
    )
    store = MarketDiscoveryStore(db)
    assert store.persist([observation]) == {
        "candidates_seen": 1, "observations_persisted": 1, "duplicates": 0,
    }
    assert store.persist([observation]) == {
        "candidates_seen": 1, "observations_persisted": 0, "duplicates": 1,
    }
    with sqlite3.connect(db) as connection:
        candidate = connection.execute(
            "SELECT candidate_state,execution_authority_state FROM market_discovery_candidates"
        ).fetchone()
        count = connection.execute("SELECT count(*) FROM market_pair_observations").fetchone()[0]
        raw = connection.execute("SELECT observation_json FROM market_pair_observations").fetchone()[0]
    assert candidate == ("unreviewed", "disabled")
    assert count == 1
    assert "must-not-persist" not in raw


def test_discovery_respects_request_caps_and_persists_provider_observations(tmp_path):
    requests = []

    async def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/token-profiles/latest/v1":
            return httpx.Response(200, json=[
                {"chainId": "base", "tokenAddress": EVM_TOKEN},
                {"chainId": "base", "tokenAddress": EVM_TOKEN},
                {"chainId": "base", "tokenAddress": "0x" + "d" * 40},
            ])
        return httpx.Response(200, json=[pair_row()])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DexScreenerDiscoveryClient(client=client)
    result = asyncio.run(collect_and_persist_market_discovery(
        str(tmp_path / "discovery.db"), client=adapter, token_limit=1,
        pair_limit_per_token=1, now=NOW,
    ))
    asyncio.run(client.aclose())
    assert len(requests) == 2
    assert result["tokens_queried"] == 1
    assert result["pairs_observed"] == 1
    assert result["observations_persisted"] == 1
    assert "/token-pairs/v1/base/" in requests[1]


def test_provider_error_does_not_include_response_body_or_url():
    async def handler(_request):
        return httpx.Response(401, text="secret body")

    adapter = DexScreenerDiscoveryClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(MarketDiscoveryError) as caught:
        asyncio.run(adapter.discover())
    assert caught.value.error_class == "provider_rejected"
    assert "secret body" not in str(caught.value)
    assert "api.dexscreener.com" not in str(caught.value)


def test_configuration_has_bounded_cadence_and_request_limits():
    MarketDiscoveryConfig(enabled=True).validate()
    with pytest.raises(ValueError, match="interval"):
        MarketDiscoveryConfig(enabled=True, interval_seconds=30).validate()
    with pytest.raises(ValueError, match="token limit"):
        MarketDiscoveryConfig(enabled=True, token_limit=21).validate()
