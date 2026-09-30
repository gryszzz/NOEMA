"""Public-address wallet balance reads, independent of every signing credential."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

import httpx

from .wallet_types import Chain

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_BITCOIN_BECH32 = re.compile(r"^bc1[ac-hj-np-z02-9]{11,71}$")
_BITCOIN_BASE58 = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
_EVM_CHAINS = {
    Chain.ETHEREUM: (1, "ethereum", "ETH", 18, "https://ethereum-rpc.publicnode.com"),
    Chain.BASE: (8453, "base", "ETH", 18, "https://mainnet.base.org"),
    Chain.POLYGON: (137, "polygon", "POL", 18, "https://polygon-bor-rpc.publicnode.com"),
}


class PublicWalletObserver:
    """Read only publicly addressable balances; this class has no key access."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(12.0))
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("wallet observer client is not open")
        return self._client

    async def read_all(self) -> list[dict[str, Any]]:
        solana = os.getenv("NOEMA_SOLANA_WALLET_ADDRESS", "").strip()
        evm = os.getenv("NOEMA_EVM_ADDRESS", "").strip()
        bitcoin = os.getenv("NOEMA_BITCOIN_ADDRESS", "").strip()
        reads = [self.read_solana(solana)]
        reads.extend(self.read_evm(chain, evm) for chain in _EVM_CHAINS)
        reads.append(self.read_bitcoin(bitcoin))
        return list(await asyncio.gather(*reads))

    async def read_solana(self, address: str) -> dict[str, Any]:
        base = {"chain": "solana", "network": "mainnet-beta", "address": address or None}
        if not address:
            return {**base, "status": "unconfigured"}
        if not _SOLANA_ADDRESS.fullmatch(address):
            return {**base, "status": "invalid_address"}
        endpoint = os.getenv("NOEMA_SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        try:
            result = await self._rpc(endpoint, "getBalance", [address, {"commitment": "confirmed"}])
            lamports = result.get("value") if isinstance(result, dict) else None
            if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports < 0:
                raise ValueError
            return {**base, "status": "read_only_balance", "source": "solana_json_rpc",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "slot": result.get("context", {}).get("slot") if isinstance(result, dict) else None,
                    "lamports": lamports,
                    "sol": str(Decimal(lamports) / Decimal(1_000_000_000)),
                    "as_of": "confirmed_rpc_response", "signer_configured": False}
        except Exception as exc:  # noqa: BLE001 - provider detail and URL may contain credentials
            return {**base, "status": "unavailable", "source": "solana_json_rpc",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "failure_type": type(exc).__name__}

    async def read_evm(self, chain: Chain, address: str) -> dict[str, Any]:
        chain_id, suffix, symbol, decimals, default_url = _EVM_CHAINS[chain]
        base = {"chain": chain.value, "network": "mainnet", "chain_id": chain_id,
                "address": address or None}
        if not address:
            return {**base, "status": "unconfigured"}
        if not _EVM_ADDRESS.fullmatch(address):
            return {**base, "status": "invalid_address"}
        endpoint = (os.getenv(f"NOEMA_EVM_RPC_URL_{suffix.upper()}")
                    or os.getenv("NOEMA_EVM_RPC_URL") or default_url)
        try:
            result = await self._rpc(endpoint, "eth_getBalance", [address, "latest"])
            if not isinstance(result, str) or not result.startswith("0x"):
                raise ValueError
            raw = int(result, 16)
            if raw < 0:
                raise ValueError
            return {**base, "status": "read_only_balance", "source": "evm_json_rpc",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "native_symbol": symbol,
                    "native_balance_wei": str(raw),
                    "native_balance": str(Decimal(raw) / (Decimal(10) ** decimals)),
                    "as_of": "latest_rpc_response", "signer_configured": False}
        except Exception as exc:  # noqa: BLE001 - do not return provider URLs or response bodies
            return {**base, "status": "unavailable", "source": "evm_json_rpc",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "failure_type": type(exc).__name__}

    async def read_bitcoin(self, address: str) -> dict[str, Any]:
        base = {"chain": "bitcoin", "network": "mainnet", "address": address or None}
        if not address:
            return {**base, "status": "unconfigured"}
        if not _valid_bitcoin_address(address):
            return {**base, "status": "invalid_address"}
        endpoint = os.getenv("NOEMA_BITCOIN_EXPLORER_URL", "https://blockstream.info/api")
        try:
            response = await self.client.get(f"{endpoint.rstrip('/')}/address/{address}")
            response.raise_for_status()
            body = response.json()
            confirmed = _stats_balance(body.get("chain_stats"))
            unconfirmed = _stats_balance(body.get("mempool_stats"), allow_negative=True)
            if confirmed is None or unconfirmed is None:
                raise ValueError
            total = confirmed + unconfirmed
            if total < 0:
                raise ValueError
            return {**base, "status": "read_only_balance", "source": "bitcoin_esplora",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "confirmed_sats": confirmed,
                    "unconfirmed_sats": unconfirmed, "total_sats": total,
                    "btc": str(Decimal(total) / Decimal(100_000_000)),
                    "as_of": "explorer_response", "signer_configured": False}
        except Exception as exc:  # noqa: BLE001 - do not return provider URLs or response bodies
            return {**base, "status": "unavailable", "source": "bitcoin_esplora",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "failure_type": type(exc).__name__}

    async def _rpc(self, endpoint: str, method: str, params: list[Any]) -> Any:
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("public RPC endpoint must use HTTPS")
        response = await self.client.post(endpoint, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("error") is not None or "result" not in body:
            raise ValueError("public RPC response was incomplete")
        return body["result"]


def _stats_balance(stats: Any, *, allow_negative: bool = False) -> int | None:
    if not isinstance(stats, dict):
        return None
    funded, spent = stats.get("funded_txo_sum"), stats.get("spent_txo_sum")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (funded, spent)):
        return None
    value = funded - spent
    return value if allow_negative or value >= 0 else None


def _valid_bitcoin_address(address: str) -> bool:
    if address.lower().startswith("bc1"):
        if address != address.lower() and address != address.upper():
            return False
        return bool(_BITCOIN_BECH32.fullmatch(address.lower()))
    return bool(_BITCOIN_BASE58.fullmatch(address))
