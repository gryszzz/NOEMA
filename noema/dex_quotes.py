"""Read-only, normalized DEX route observations for strategy research.

This module never builds, signs, or submits a transaction. Provider responses may
contain transaction calldata; adapters intentionally discard it at the boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

EVM_CHAIN_IDS = {
    "ethereum": 1, "base": 8453, "arbitrum": 42161,
    "optimism": 10, "polygon": 137, "bnb-chain": 56, "avalanche": 43114,
}
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _valid_solana_address(address: str) -> bool:
    if not 32 <= len(address) <= 44 or any(ch not in _BASE58_ALPHABET for ch in address):
        return False
    number = 0
    for character in address:
        number = number * 58 + _BASE58_ALPHABET.index(character)
    decoded = (number.bit_length() + 7) // 8
    leading_zeroes = len(address) - len(address.lstrip("1"))
    return decoded + leading_zeroes == 32


class DexQuoteError(RuntimeError):
    """Safe provider error; deliberately excludes response bodies and URLs."""

    def __init__(self, provider: str, reason: str, *, http_status: int | None = None):
        self.provider = provider
        self.reason = reason
        self.http_status = http_status
        super().__init__(f"{provider}:{reason}" + (f":{http_status}" if http_status else ""))


@dataclass(frozen=True)
class DexQuoteRequest:
    chain: str
    sell_asset: str
    buy_asset: str
    sell_amount_atomic: int
    sell_decimals: int
    buy_decimals: int
    taker_address: str
    max_slippage_bps: int

    def validate(self) -> None:
        if self.chain not in {"solana", *EVM_CHAIN_IDS}:
            raise ValueError("unsupported DEX quote chain")
        if not self.sell_asset.strip() or not self.buy_asset.strip() or self.sell_asset == self.buy_asset:
            raise ValueError("DEX quote assets must be distinct and nonempty")
        if self.chain == "solana" and any(
            not _valid_solana_address(asset) for asset in (self.sell_asset, self.buy_asset)
        ):
            raise ValueError("Solana token mints must be valid base58 public keys")
        if self.chain in EVM_CHAIN_IDS and any(
            re.fullmatch(r"0x[0-9a-fA-F]{40}", asset) is None
            for asset in (self.sell_asset, self.buy_asset)
        ):
            raise ValueError("EVM tokens must be explicit contract addresses")
        if (isinstance(self.sell_amount_atomic, bool)
                or not isinstance(self.sell_amount_atomic, int)
                or self.sell_amount_atomic <= 0):
            raise ValueError("sell amount must be a positive integer")
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 36
               for v in (self.sell_decimals, self.buy_decimals)):
            raise ValueError("token decimals are outside supported bounds")
        if (isinstance(self.max_slippage_bps, bool)
                or not isinstance(self.max_slippage_bps, int)
                or not 0 <= self.max_slippage_bps <= 10_000):
            raise ValueError("slippage limit is outside supported bounds")
        if not self.taker_address.strip():
            raise ValueError("a quote identity is required")
        if self.chain == "solana" and not _valid_solana_address(self.taker_address):
            raise ValueError("Solana taker address has invalid length")
        if self.chain in EVM_CHAIN_IDS and (
            re.fullmatch(r"0x[0-9a-fA-F]{40}", self.taker_address) is None
        ):
            raise ValueError("EVM taker address is malformed")


@dataclass(frozen=True)
class NormalizedDexQuote:
    provider: str
    chain: str
    chain_id: int | None
    sell_asset: str
    buy_asset: str
    sell_decimals: int
    buy_decimals: int
    sell_amount_atomic: str
    buy_amount_atomic: str | None
    minimum_buy_amount_atomic: str | None
    observed_at: str
    observed_quote_expires_at: str
    observed_quote_ttl_seconds: int
    provider_latency_ms: int
    estimated_slippage_bps: str | None
    price_impact_pct: str | None
    network_fee_amount_atomic: str | None
    network_fee_asset: str | None
    provider_fee_amount_atomic: str | None
    provider_fee_asset: str | None
    additional_fees: tuple[tuple[str, str, str], ...]
    gas_units: str | None
    gas_price_atomic: str | None
    liquidity_available: bool | None
    depth_amount: str | None
    route_sources: tuple[str, ...]
    simulation_status: str
    provider_reference: str | None
    evidence_sha256: str
    provider_preflight_status: str
    provider_fee_bps: str | None = None
    provider_expiry_kind: str | None = None
    provider_expiry_value: str | None = None
    provider_route: str | None = None
    live_execution_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DexQuoteAdapter(Protocol):
    async def quote(self, request: DexQuoteRequest) -> NormalizedDexQuote: ...


def _str_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _slippage_bps(expected: str | None, minimum: str | None) -> str | None:
    if expected is None or minimum is None:
        return None
    try:
        expected_d, minimum_d = Decimal(expected), Decimal(minimum)
        if not expected_d.is_finite() or expected_d <= 0 or not minimum_d.is_finite():
            return None
        return str(max(Decimal(0), (expected_d - minimum_d) / expected_d * Decimal(10_000)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _quote(
    request: DexQuoteRequest, *, provider: str,
    buy_amount: object = None, minimum_buy_amount: object = None,
    price_impact_pct: object = None, network_fee_amount: object = None,
    network_fee_asset: object = None, gas_units: object = None, gas_price: object = None,
    liquidity_available: bool | None = None, depth_amount: object = None,
    route_sources: tuple[str, ...] = (), provider_reference: object = None,
    fee_amount: object = None, fee_asset: object = None, ttl_seconds: int = 5,
    fee_bps: object = None, provider_expiry_kind: str | None = None,
    provider_expiry_value: object = None, provider_route: object = None,
    provider_latency_ms: int = 0,
    additional_fees: tuple[tuple[str, str, str], ...] = (),
    provider_preflight_status: str = "not_reported",
) -> NormalizedDexQuote:
    now = datetime.now(UTC)
    sell_amount = str(request.sell_amount_atomic)
    buy = _str_value(buy_amount)
    minimum = _str_value(minimum_buy_amount)
    # Hash only normalized, non-secret quote evidence. Never persist the provider
    # object because Jupiter/0x may include a transaction or calldata in it.
    safe_payload = {
        "provider": provider, "chain": request.chain, "sell_asset": request.sell_asset,
        "buy_asset": request.buy_asset, "sell_amount_atomic": sell_amount,
        "buy_amount_atomic": buy, "minimum_buy_amount_atomic": minimum,
        "price_impact_pct": _str_value(price_impact_pct),
        "network_fee_amount_atomic": _str_value(network_fee_amount),
        "network_fee_asset": _str_value(network_fee_asset),
        "provider_fee_amount_atomic": _str_value(fee_amount),
        "provider_fee_asset": _str_value(fee_asset),
        "provider_fee_bps": _str_value(fee_bps),
        "additional_fees": additional_fees,
        "provider_expiry_kind": provider_expiry_kind,
        "provider_expiry_value": _str_value(provider_expiry_value),
        "provider_route": _str_value(provider_route),
        "provider_preflight_status": provider_preflight_status,
        "observed_at": now.isoformat(),
        "sell_decimals": request.sell_decimals, "buy_decimals": request.buy_decimals,
        "provider_latency_ms": provider_latency_ms,
        "gas_units": _str_value(gas_units), "gas_price_atomic": _str_value(gas_price),
        "route_sources": route_sources,
    }
    digest = hashlib.sha256(json.dumps(safe_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return NormalizedDexQuote(
        provider=provider, chain=request.chain, chain_id=EVM_CHAIN_IDS.get(request.chain),
        sell_asset=request.sell_asset, buy_asset=request.buy_asset,
        sell_decimals=request.sell_decimals, buy_decimals=request.buy_decimals,
        sell_amount_atomic=sell_amount, buy_amount_atomic=buy,
        minimum_buy_amount_atomic=minimum, observed_at=now.isoformat(),
        observed_quote_expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        observed_quote_ttl_seconds=ttl_seconds,
        provider_latency_ms=max(0, provider_latency_ms),
        estimated_slippage_bps=_slippage_bps(buy, minimum),
        price_impact_pct=_str_value(price_impact_pct),
        network_fee_amount_atomic=_str_value(network_fee_amount),
        network_fee_asset=_str_value(network_fee_asset), gas_units=_str_value(gas_units),
        provider_fee_amount_atomic=_str_value(fee_amount),
        provider_fee_asset=_str_value(fee_asset),
        additional_fees=additional_fees,
        gas_price_atomic=_str_value(gas_price), liquidity_available=liquidity_available,
        depth_amount=_str_value(depth_amount), route_sources=route_sources,
        simulation_status="not_run", provider_reference=_str_value(provider_reference),
        evidence_sha256=digest, provider_preflight_status=provider_preflight_status,
        provider_fee_bps=_str_value(fee_bps),
        provider_expiry_kind=provider_expiry_kind,
        provider_expiry_value=_str_value(provider_expiry_value),
        provider_route=_str_value(provider_route),
    )


class JupiterSwapV2QuoteAdapter:
    """Read Jupiter Swap V2 /order; only normalized quote fields leave this class."""

    BASE_URL = "https://api.jup.ag/swap/v2/order"

    def __init__(self, *, api_key: str | None = None, client: httpx.AsyncClient | None = None,
                 quote_ttl_seconds: int = 5) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("JUPITER_API_KEY")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.quote_ttl_seconds = max(1, min(30, quote_ttl_seconds))

    async def close(self) -> None:
        await self.client.aclose()

    async def quote(self, request: DexQuoteRequest) -> NormalizedDexQuote:
        request.validate()
        if request.chain != "solana":
            raise ValueError("Jupiter quote adapter only supports Solana")
        params = {
            "inputMint": request.sell_asset, "outputMint": request.buy_asset,
            "amount": str(request.sell_amount_atomic), "taker": request.taker_address,
            "slippageBps": str(request.max_slippage_bps),
        }
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        started = time.monotonic()
        payload = await self._get(params, headers)
        latency_ms = round((time.monotonic() - started) * 1000)
        platform_fee = payload.get("platformFee") if isinstance(payload.get("platformFee"), dict) else {}
        transaction = payload.get("transaction")
        preflight = (
            "provider_order_not_buildable" if transaction == "" else
            "provider_order_available" if isinstance(transaction, str) and transaction else
            "price_only_no_transaction"
        )
        provider_expiry_kind = (
            "rfq_expire_at" if payload.get("expireAt") is not None else
            "last_valid_block_height" if payload.get("lastValidBlockHeight") is not None else None
        )
        provider_expiry_value = payload.get("expireAt") or payload.get("lastValidBlockHeight")
        router = payload.get("router")
        route = payload.get("routePlan")
        sources = tuple(sorted({str(item.get("swapInfo", {}).get("label"))
                                for item in route if isinstance(item, dict)
                                and isinstance(item.get("swapInfo"), dict)
                                and item["swapInfo"].get("label")})) if isinstance(route, list) else (
                                    (str(router),) if isinstance(router, str) and router else ()
                                )
        fee_mint = platform_fee.get("feeMint") or payload.get("feeMint")
        platform_fees = (
            (("jupiter_platform_fee", str(platform_fee["amount"]), str(fee_mint)),)
            if platform_fee.get("amount") is not None and fee_mint else ()
        )
        return _quote(
            request, provider="jupiter_swap_v2",
            buy_amount=payload.get("outAmount") or payload.get("outputAmount"),
            minimum_buy_amount=payload.get("otherAmountThreshold") or payload.get("minimumOutputAmount"),
            price_impact_pct=payload.get("priceImpactPct"),
            fee_amount=platform_fee.get("amount"), fee_asset=fee_mint,
            fee_bps=payload.get("feeBps"), additional_fees=platform_fees,
            route_sources=sources,
            provider_reference=payload.get("requestId"),
            provider_expiry_kind=provider_expiry_kind,
            provider_expiry_value=provider_expiry_value,
            provider_route=router,
            ttl_seconds=self.quote_ttl_seconds,
            provider_latency_ms=latency_ms,
            provider_preflight_status=preflight,
        )

    async def _get(self, params: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.client.get(self.BASE_URL, params=params, headers=headers)
            if response.status_code >= 400:
                raise DexQuoteError("jupiter_swap_v2", "provider_rejected", http_status=response.status_code)
            payload = response.json()
        except DexQuoteError:
            raise
        except httpx.HTTPError:
            raise DexQuoteError("jupiter_swap_v2", "network_failure") from None
        except ValueError:
            raise DexQuoteError("jupiter_swap_v2", "malformed_response") from None
        if not isinstance(payload, dict) or not (payload.get("outAmount") or payload.get("outputAmount")):
            raise DexQuoteError("jupiter_swap_v2", "quote_unavailable")
        return payload


class ZeroExPriceQuoteAdapter:
    """Read-only 0x Swap API v2 /price adapter for supported EVM networks."""

    BASE_URL = "https://api.0x.org/swap/allowance-holder/price"

    def __init__(self, *, api_key: str | None = None, client: httpx.AsyncClient | None = None,
                 quote_ttl_seconds: int = 5) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("ZEROX_API_KEY")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.quote_ttl_seconds = max(1, min(30, quote_ttl_seconds))

    async def close(self) -> None:
        await self.client.aclose()

    async def quote(self, request: DexQuoteRequest) -> NormalizedDexQuote:
        request.validate()
        if request.chain not in EVM_CHAIN_IDS:
            raise ValueError("0x price adapter only supports the configured EVM network registry")
        if not self.api_key:
            raise DexQuoteError("0x", "api_key_missing")
        params = {
            "chainId": str(EVM_CHAIN_IDS[request.chain]), "sellToken": request.sell_asset,
            "buyToken": request.buy_asset, "sellAmount": str(request.sell_amount_atomic),
            "taker": request.taker_address,
        }
        started = time.monotonic()
        try:
            response = await self.client.get(
                self.BASE_URL, params=params,
                headers={"0x-api-key": self.api_key, "0x-version": "v2"},
            )
            if response.status_code >= 400:
                raise DexQuoteError("0x", "provider_rejected", http_status=response.status_code)
            payload = response.json()
        except DexQuoteError:
            raise
        except httpx.HTTPError:
            raise DexQuoteError("0x", "network_failure") from None
        except ValueError:
            raise DexQuoteError("0x", "malformed_response") from None
        latency_ms = round((time.monotonic() - started) * 1000)
        if not isinstance(payload, dict) or not payload.get("buyAmount"):
            raise DexQuoteError("0x", "quote_unavailable")
        route = payload.get("route") or {}
        fills = route.get("fills", []) if isinstance(route, dict) else []
        sources = tuple(sorted({str(item.get("source")) for item in fills
                                if isinstance(item, dict) and item.get("source")}))
        fees = payload.get("fees") if isinstance(payload.get("fees"), dict) else {}
        fee = fees.get("zeroExFee") if isinstance(fees.get("zeroExFee"), dict) else {}
        normalized_fees = tuple(
            (kind, str(item.get("amount")), str(item.get("token")))
            for kind in ("zeroExFee", "integratorFee", "gasFee")
            if isinstance(fees.get(kind), dict)
            for item in (fees[kind],)
            if item.get("amount") is not None and item.get("token") is not None
        )
        # 0x says simulationIncomplete may be ignored for the indicative
        # /price endpoint. Price is not an executable quote or simulation.
        preflight = "indicative_price_only"
        return _quote(
            request, provider="0x_swap_v2_price",
            buy_amount=payload.get("buyAmount"), minimum_buy_amount=payload.get("minBuyAmount"),
            network_fee_amount=payload.get("totalNetworkFee"),
            network_fee_asset={
                "ethereum": "ETH", "base": "ETH", "arbitrum": "ETH",
                "optimism": "ETH", "polygon": "POL", "bnb-chain": "BNB",
                "avalanche": "AVAX",
            }[request.chain],
            gas_units=payload.get("gas"), gas_price=payload.get("gasPrice"),
            liquidity_available=payload.get("liquidityAvailable"),
            route_sources=sources, provider_reference=payload.get("zid"),
            fee_amount=fee.get("amount"), fee_asset=fee.get("token"),
            ttl_seconds=self.quote_ttl_seconds,
            provider_latency_ms=latency_ms,
            additional_fees=normalized_fees,
            provider_preflight_status=preflight,
        )


def persist_dex_quote(database: str, quote: NormalizedDexQuote) -> bool:
    """Append one normalized quote observation, omitting provider tx/calldata blobs."""
    with sqlite3.connect(database, timeout=10.0) as connection:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS dex_quote_observations (
                evidence_sha256 TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                chain TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                observed_quote_expires_at TEXT NOT NULL,
                quote_json TEXT NOT NULL
            )
        """)
        cursor = connection.execute(
            "INSERT OR IGNORE INTO dex_quote_observations "
            "(evidence_sha256,provider,chain,observed_at,observed_quote_expires_at,quote_json) "
            "VALUES(?,?,?,?,?,?)",
            (quote.evidence_sha256, quote.provider, quote.chain, quote.observed_at,
             quote.observed_quote_expires_at,
             json.dumps(quote.as_dict(), sort_keys=True, separators=(",", ":"))),
        )
        return cursor.rowcount == 1


async def collect_dex_quote(
    database: str, adapter: DexQuoteAdapter, request: DexQuoteRequest,
) -> NormalizedDexQuote:
    """Collect one current quote and durably retain its normalized evidence."""
    quote = await adapter.quote(request)
    persist_dex_quote(database, quote)
    return quote


def compare_route_quotes(
    quotes: list[NormalizedDexQuote], *, now: datetime | None = None,
) -> dict[str, Any]:
    """Compare gross quoted output only when observations are like-for-like and fresh.

    Network fees and provider fees are reported as incomplete costs unless they
    can be converted to the same output denomination; this helper never calls a
    gross-output winner an economic winner.
    """
    now = now or datetime.now(UTC)
    eligible: list[NormalizedDexQuote] = []
    rejected: list[dict[str, str]] = []
    identity: tuple[str, str, int, str, int, str] | None = None
    for quote in quotes:
        try:
            observed = datetime.fromisoformat(quote.observed_at)
            expires = datetime.fromisoformat(quote.observed_quote_expires_at)
        except (TypeError, ValueError):
            rejected.append({"provider": quote.provider, "reason": "invalid_quote_timestamp"})
            continue
        key = (quote.chain, quote.sell_asset, quote.sell_decimals,
               quote.buy_asset, quote.buy_decimals, quote.sell_amount_atomic)
        if observed.tzinfo is None or expires.tzinfo is None or now >= expires:
            rejected.append({"provider": quote.provider, "reason": "expired_or_unzoned_quote"})
            continue
        if quote.buy_amount_atomic is None:
            rejected.append({"provider": quote.provider, "reason": "output_unavailable"})
            continue
        try:
            amount = int(quote.buy_amount_atomic)
        except (TypeError, ValueError):
            rejected.append({"provider": quote.provider, "reason": "invalid_output_amount"})
            continue
        if amount <= 0:
            rejected.append({"provider": quote.provider, "reason": "invalid_output_amount"})
            continue
        if identity is not None and key != identity:
            rejected.append({"provider": quote.provider, "reason": "non_comparable_request"})
            continue
        if identity is None:
            identity = key
        eligible.append(quote)
    if not eligible:
        return {"status": "no_fresh_comparable_quotes", "gross_output_winner": None,
                "cost_completeness": "unavailable", "rejected": rejected}
    winner = max(eligible, key=lambda quote: int(quote.buy_amount_atomic or "0"))
    complete_costs = all(
        quote.network_fee_amount_atomic is not None
        and quote.network_fee_asset == quote.buy_asset
        and (quote.provider_fee_amount_atomic is None
             or quote.provider_fee_asset == quote.buy_asset)
        for quote in eligible
    )
    return {
        "status": "compared",
        "gross_output_winner": winner.provider,
        "gross_output_atomic": winner.buy_amount_atomic,
        "cost_completeness": "complete_in_output_asset" if complete_costs else "partial",
        "economic_winner": winner.provider if complete_costs else None,
        "eligible_providers": [quote.provider for quote in eligible],
        "rejected": rejected,
    }
