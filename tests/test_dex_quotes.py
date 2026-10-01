from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from noema.dex_quotes import (
    DexQuoteError,
    DexQuoteRequest,
    JupiterSwapV2QuoteAdapter,
    NormalizedDexQuote,
    ZeroExPriceQuoteAdapter,
    compare_route_quotes,
    persist_dex_quote,
)


def request(chain: str = "base") -> DexQuoteRequest:
    return DexQuoteRequest(
        chain=chain, sell_asset="0x" + "1" * 40 if chain != "solana" else "So11111111111111111111111111111111111111112",
        buy_asset="0x" + "2" * 40 if chain != "solana" else "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        sell_amount_atomic=1_000_000, sell_decimals=6, buy_decimals=6,
        taker_address="0x" + "a" * 40 if chain != "solana" else "11111111111111111111111111111111",
        max_slippage_bps=50,
    )


def test_zeroex_quote_is_normalized_and_transaction_payload_is_discarded():
    response_payload = {
        "buyAmount": "990000", "minBuyAmount": "985050", "totalNetworkFee": "1200",
        "gas": "90000", "gasPrice": "4", "liquidityAvailable": True,
        "fees": {
            "zeroExFee": {"amount": "100", "token": "0x" + "2" * 40},
            "integratorFee": {"amount": "4", "token": "0x" + "2" * 40},
            "gasFee": None,
        },
        "issues": {"simulationIncomplete": False},
        "route": {"fills": [{"source": "Uniswap_V3"}]}, "zid": "safe-quote-id",
        "transaction": {"data": "sensitive-calldata-must-not-escape"},
    }

    async def handler(_request):
        return httpx.Response(200, json=response_payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ZeroExPriceQuoteAdapter(api_key="must-not-escape", client=client)
    quote = asyncio.run(adapter.quote(request("base")))
    serialized = json.dumps(quote.as_dict())
    assert quote.chain_id == 8453
    assert quote.buy_amount_atomic == "990000"
    assert quote.minimum_buy_amount_atomic == "985050"
    assert float(quote.estimated_slippage_bps) == 50
    assert quote.network_fee_asset == "ETH"
    assert quote.provider_fee_amount_atomic == "100"
    assert quote.additional_fees == (
        ("zeroExFee", "100", "0x" + "2" * 40),
        ("integratorFee", "4", "0x" + "2" * 40),
    )
    assert quote.provider_preflight_status == "indicative_price_only"
    assert quote.route_sources == ("Uniswap_V3",)
    assert quote.simulation_status == "not_run"
    assert quote.live_execution_enabled is False
    assert "sensitive-calldata" not in serialized
    assert "must-not-escape" not in serialized


def test_jupiter_quote_uses_v2_read_only_order_and_discards_transaction():
    response_payload = {
        "outAmount": "950000", "otherAmountThreshold": "940500",
        "priceImpactPct": "0.002", "requestId": "jupiter-request",
        "router": "metis", "transaction": "secret-unsigned-transaction",
        "feeBps": 12, "feeMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "platformFee": {"amount": "120", "feeBps": 5,
                         "feeMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
        "lastValidBlockHeight": 123456,
        "routePlan": [{"swapInfo": {"label": "Raydium"}}],
    }
    seen: dict[str, object] = {}

    async def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=response_payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    quote = asyncio.run(JupiterSwapV2QuoteAdapter(client=client).quote(request("solana")))
    assert "/swap/v2/order" in str(seen["url"])
    assert quote.provider == "jupiter_swap_v2"
    assert quote.route_sources == ("Raydium",)
    assert float(quote.estimated_slippage_bps) == 100
    assert quote.provider_route == "metis"
    assert quote.provider_fee_bps == "12"
    assert quote.provider_fee_amount_atomic == "120"
    assert quote.provider_expiry_kind == "last_valid_block_height"
    assert quote.provider_expiry_value == "123456"
    assert quote.provider_preflight_status == "provider_order_available"
    assert "secret-unsigned-transaction" not in json.dumps(quote.as_dict())


def test_quote_provider_errors_do_not_expose_response_body():
    async def handler(_request):
        return httpx.Response(401, text="sensitive provider response")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ZeroExPriceQuoteAdapter(api_key="secret", client=client)
    with pytest.raises(DexQuoteError) as caught:
        asyncio.run(adapter.quote(request()))
    assert str(caught.value) == "0x:provider_rejected:401"
    assert "sensitive" not in str(caught.value)


def test_quote_request_rejects_noninteger_amounts_and_malformed_chain_identity():
    from dataclasses import replace

    with pytest.raises(ValueError, match="positive integer"):
        replace(request(), sell_amount_atomic=1.5).validate()
    with pytest.raises(ValueError, match="EVM taker address"):
        replace(request(), taker_address="0xnot-an-address".ljust(42, "0")).validate()
    with pytest.raises(ValueError, match="Solana taker address"):
        replace(request("solana"), taker_address="invalid").validate()


def test_quote_persistence_is_immutable_and_never_stores_provider_payload(tmp_path):
    now = datetime.now(UTC)
    quote = NormalizedDexQuote(
        provider="0x_swap_v2_price", chain="base", chain_id=8453,
        sell_asset="token-a", buy_asset="token-b", sell_decimals=6, buy_decimals=6,
        sell_amount_atomic="100",
        buy_amount_atomic="99", minimum_buy_amount_atomic="98", observed_at=now.isoformat(),
        observed_quote_expires_at=(now + timedelta(seconds=5)).isoformat(),
        observed_quote_ttl_seconds=5, provider_latency_ms=100, estimated_slippage_bps="101.01",
        price_impact_pct=None, network_fee_amount_atomic="4", network_fee_asset="ETH",
        provider_fee_amount_atomic=None, provider_fee_asset=None, additional_fees=(), gas_units="21000",
        gas_price_atomic="1", liquidity_available=True, depth_amount=None,
        route_sources=("Uniswap",), simulation_status="not_run", provider_reference="id",
        evidence_sha256="a" * 64, provider_preflight_status="not_reported",
    )
    database = str(tmp_path / "quotes.db")
    assert persist_dex_quote(database, quote) is True
    assert persist_dex_quote(database, quote) is False
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT quote_json FROM dex_quote_observations").fetchone()
    assert "transaction" not in row[0]


def test_route_comparison_reports_gross_winner_without_inventing_net_edge():
    base = request("base")
    now = datetime.now(UTC)

    def quote(provider: str, output: str) -> NormalizedDexQuote:
        return NormalizedDexQuote(
            provider=provider, chain="base", chain_id=8453,
            sell_asset=base.sell_asset, buy_asset=base.buy_asset,
            sell_decimals=base.sell_decimals, buy_decimals=base.buy_decimals,
            sell_amount_atomic=str(base.sell_amount_atomic), buy_amount_atomic=output,
            minimum_buy_amount_atomic=None, observed_at=now.isoformat(),
            observed_quote_expires_at=(now + timedelta(seconds=5)).isoformat(),
            observed_quote_ttl_seconds=5, provider_latency_ms=100, estimated_slippage_bps=None,
            price_impact_pct=None, network_fee_amount_atomic="10", network_fee_asset="ETH",
            provider_fee_amount_atomic=None, provider_fee_asset=None, additional_fees=(), gas_units="21000",
            gas_price_atomic="1", liquidity_available=True, depth_amount=None,
            route_sources=(), simulation_status="not_run", provider_reference=None,
            evidence_sha256=provider, provider_preflight_status="not_reported",
        )

    result = compare_route_quotes([quote("route-a", "99"), quote("route-b", "101")], now=now)
    assert result["gross_output_winner"] == "route-b"
    assert result["economic_winner"] is None
    assert result["cost_completeness"] == "partial"
