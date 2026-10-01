"""Public-address wallet balance reads, independent of every signing credential."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

import httpx

from .chain_registry import EVMChain, load_evm_chains

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_BITCOIN_BECH32 = re.compile(r"^bc1[ac-hj-np-z02-9]{11,71}$")
_BITCOIN_BASE58 = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
_EVM_NATIVE_DECIMALS = 18
_MAX_EVM_BLOCK_AGE_SECONDS = 120


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
        try:
            chains = load_evm_chains()
        except (TypeError, ValueError) as exc:
            reads.append(asyncio.sleep(0, result={
                "chain": "evm_registry", "chain_family": "evm",
                "status": "registry_invalid", "rpc_health": "misconfigured",
                "failure_type": type(exc).__name__, "execution_authority_state": "disabled",
            }))
            reads.append(self.read_bitcoin(bitcoin))
            return list(await asyncio.gather(*reads))
        reads.extend(self.read_evm(chain, evm) for chain in chains)
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

    async def read_evm(self, chain: EVMChain, address: str) -> dict[str, Any]:
        invalid_address = bool(address) and _EVM_ADDRESS.fullmatch(address) is None
        base = {
            **chain.public_record(), "network": "mainnet", "address": address or None,
            "wallet_address_configured": bool(address),
            "status": "unconfigured", "rpc_health": "unknown",
            "data_freshness": "unknown", "source": "evm_json_rpc",
        }
        endpoint = chain.rpc_endpoint
        if not endpoint:
            return {**base, "status": "rpc_unconfigured", "rpc_health": "unconfigured"}
        observed_at = datetime.now(UTC)
        try:
            actual_chain_id = _rpc_integer(await self._rpc(endpoint, "eth_chainId", []))
            if actual_chain_id != chain.chain_id:
                return {
                    **base, "status": "rpc_chain_id_mismatch", "rpc_health": "misconfigured",
                    "actual_chain_id": actual_chain_id,
                    "observed_at": observed_at.isoformat(),
                }
            block = await self._rpc(endpoint, "eth_getBlockByNumber", ["latest", False])
            if not isinstance(block, dict):
                raise TypeError
            block_number = _rpc_integer(block.get("number"))
            block_timestamp = _rpc_integer(block.get("timestamp"))
            block_at = datetime.fromtimestamp(block_timestamp, UTC)
            age = max(0.0, (observed_at - block_at).total_seconds())
            freshness = "fresh" if age <= _MAX_EVM_BLOCK_AGE_SECONDS else "stale"
            if invalid_address:
                return {
                    **base, "status": "invalid_address", "rpc_health": "healthy",
                    "latest_block_number": block_number,
                    "latest_block_at": block_at.isoformat(),
                    "block_age_seconds": round(age, 3), "data_freshness": freshness,
                    "observed_at": observed_at.isoformat(),
                    "provenance": "eth_chainId + eth_getBlockByNumber",
                }
            balance_raw: int | None = None
            if address:
                result = await self._rpc(
                    endpoint, "eth_getBalance", [address, hex(block_number)],
                )
                balance_raw = _rpc_integer(result)
            row = {
                **base,
                "status": "read_only_balance" if address else "network_observed",
                "rpc_health": "healthy",
                "latest_block_number": block_number,
                "latest_block_at": block_at.isoformat(),
                "block_age_seconds": round(age, 3),
                "data_freshness": freshness,
                "capability_state": (
                    "read_only_wallet_observation" if address else "network_observed_no_wallet"
                ),
                "observed_at": observed_at.isoformat(),
                "as_of": f"block:{block_number}",
                "provenance": "eth_chainId + eth_getBlockByNumber + eth_getBalance",
                "signer_configured": False,
            }
            if balance_raw is not None:
                row.update({
                    "native_balance_wei": str(balance_raw),
                    "native_balance": str(Decimal(balance_raw) / (Decimal(10) ** _EVM_NATIVE_DECIMALS)),
                })
            return row
        except (OverflowError, OSError, ValueError, TypeError, httpx.HTTPError) as exc:
            return {
                **base, "status": "unavailable", "rpc_health": "unavailable",
                "observed_at": observed_at.isoformat(), "failure_type": type(exc).__name__,
            }
        except Exception as exc:  # noqa: BLE001 - never expose provider URL or response data.
            return {
                **base, "status": "unavailable", "rpc_health": "unavailable",
                "observed_at": observed_at.isoformat(), "failure_type": type(exc).__name__,
            }

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


def _rpc_integer(value: object) -> int:
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]+", value) is None:
        raise ValueError("EVM RPC integer is invalid")
    parsed = int(value, 16)
    if parsed < 0:
        raise ValueError("EVM RPC integer is negative")
    return parsed


def _valid_bitcoin_address(address: str) -> bool:
    if address.lower().startswith("bc1"):
        if address != address.lower() and address != address.upper():
            return False
        return bool(_BITCOIN_BECH32.fullmatch(address.lower()))
    return bool(_BITCOIN_BASE58.fullmatch(address))
