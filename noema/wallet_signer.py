from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any, ClassVar, Protocol

import httpx

from .wallet_intents import WalletExecutionReceipt, WalletIntent
from .wallet_types import Chain


class WalletSigner(Protocol):
    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt: ...


async def _read_evm_token_balances(indexer_url: str, address: str) -> list[dict[str, Any]] | None:
    """Read public token holdings outside the Keychain signer child as a fallback."""
    if not indexer_url.startswith("https://") or len(address) != 42 or not address.startswith("0x"):
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{indexer_url.rstrip('/')}/addresses/{address}/token-balances"
            )
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list):
            return None
        tokens = []
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            token = row.get("token") or {}
            if not isinstance(token, dict):
                continue
            tokens.append({
                "contract": token.get("address_hash"),
                "symbol": token.get("symbol"),
                "decimals": token.get("decimals"),
                "raw_amount": str(row.get("value", "0")),
            })
        return tokens
    except (httpx.HTTPError, ValueError, TypeError):
        return None


class DisabledWalletSigner:
    """Fail-closed signer used until a programmable wallet provider is configured."""

    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        raise RuntimeError(
            f"wallet execution disabled for intent {intent.intent_id}; "
            "configure a policy-enforced signer backend first"
        )


class LocalSolanaWalletSigner:
    """One-shot Keychain-backed signer; key material stays in its child process."""

    DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        expected_address: str | None = None,
        enabled: bool | None = None,
        master_halt: bool | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.rpc_url = rpc_url or os.environ.get("NOEMA_SOLANA_RPC_URL") or self.DEFAULT_RPC_URL
        self.expected_address = expected_address or os.environ.get("NOEMA_SOLANA_WALLET_ADDRESS")
        self.enabled = (
            os.environ.get("NOEMA_SOLANA_SIGNER_ENABLED") == "1"
            if enabled is None else enabled
        )
        self.master_halt = (
            os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
            or os.environ.get("NOEMA_SOLANA_MASTER_HALT", "1") != "0"
            if master_halt is None else master_halt
        )
        self.python_executable = python_executable or sys.executable

    async def inspect(self) -> dict[str, Any]:
        return await self._run({"operation": "inspect", "expected_address": self.expected_address})

    async def balances(self) -> dict[str, Any]:
        return await self._run({
            "operation": "balances",
            "expected_address": self.expected_address,
            "rpc_url": self.rpc_url,
        })

    async def simulate(self, intent: WalletIntent) -> dict[str, Any]:
        payload = self._intent_payload(intent)
        payload["operation"] = "simulate_native_transfer"
        return await self._run(payload)

    async def sign_dry_run(self, intent: WalletIntent) -> dict[str, Any]:
        payload = self._intent_payload(intent)
        payload["operation"] = "solana_sign_dry_run"
        return await self._run(payload)

    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        if not self.enabled or self.master_halt:
            raise RuntimeError("Solana signer disabled or owner master halt enabled")
        payload = self._intent_payload(intent)
        payload.update({
            "operation": "execute_native_transfer",
            "signer_enabled": self.enabled,
            "master_halt": self.master_halt,
        })
        result = await self._run(payload)
        return WalletExecutionReceipt(
            intent_id=intent.intent_id,
            provider_reference="solana-keychain-signer",
            chain=Chain.SOLANA,
            transaction_reference=result.get("signature"),
            submitted=result.get("status") in {"confirmed", "submitted"},
            status=str(result.get("status", "unknown")),
            fee_lamports=result.get("fee_lamports"),
            slot=result.get("slot"),
            pre_balance_lamports=result.get("pre_balance_lamports"),
            post_balance_lamports=result.get("post_balance_lamports"),
        )

    def _intent_payload(self, intent: WalletIntent) -> dict[str, Any]:
        if intent.chain is not Chain.SOLANA:
            raise RuntimeError("Solana signer received a different chain")
        if intent.action != "sol_transfer":
            raise RuntimeError("Solana signer supports only structured sol_transfer intents")
        if not intent.mission_id or not intent.evidence_ids:
            raise RuntimeError("Solana transfer requires a mission and evidence references")
        if not intent.destination or intent.amount_atomic is None:
            raise RuntimeError("Solana transfer intent is incomplete")
        if intent.fee_limit_lamports is None:
            raise RuntimeError("Solana transfer requires an explicit fee ceiling")
        return {
            "source": self.expected_address,
            "destination": intent.destination,
            "amount_lamports": intent.amount_atomic,
            "fee_limit_lamports": intent.fee_limit_lamports,
            "expected_address": self.expected_address,
            "rpc_url": self.rpc_url,
        }

    async def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_sync, request)

    def _run_sync(self, request: dict[str, Any]) -> dict[str, Any]:
        # The child receives no parent environment credentials. It can read only
        # its own Keychain item and the explicit structured request.
        child_env = {
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": os.environ.get("HOME", ""),
        }
        try:
            result = subprocess.run(
                [self.python_executable, "-m", "noema.wallet_signer_process"],
                input=json.dumps(request, separators=(",", ":")),
                text=True,
                capture_output=True,
                env=child_env,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("isolated wallet signer process unavailable") from None
        try:
            response = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise RuntimeError("isolated wallet signer returned no structured result") from None
        if result.returncode != 0 or not isinstance(response, dict) or "error" in response:
            reason = response.get("error") if isinstance(response, dict) else None
            # Keep only the deliberately sanitized signer reason.
            if not isinstance(reason, str) or len(reason) > 100:
                reason = "wallet signer operation failed"
            raise RuntimeError(reason)
        return response


class LocalEvmWalletSigner:
    """Shared EVM key identity with RPC/network isolation by configured Chain."""

    CHAIN_CONFIG: ClassVar[dict[Chain, tuple[int, str, str]]] = {
        Chain.ETHEREUM: (1, "https://ethereum.publicnode.com", "ethereum"),
        Chain.BASE: (8453, "https://base-rpc.publicnode.com", "base"),
        Chain.POLYGON: (137, "https://polygon-bor-rpc.publicnode.com", "polygon"),
    }

    def __init__(
        self,
        *,
        expected_address: str | None = None,
        enabled: bool | None = None,
        master_halt: bool | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.expected_address = expected_address or os.environ.get("NOEMA_EVM_ADDRESS")
        self.enabled = os.environ.get("NOEMA_EVM_SIGNER_ENABLED") == "1" if enabled is None else enabled
        self.master_halt = (
            os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
            or os.environ.get("NOEMA_EVM_MASTER_HALT", "1") != "0"
            if master_halt is None else master_halt
        )
        self.python_executable = python_executable or sys.executable

    def chain_config(self, chain: Chain) -> tuple[int, str, str]:
        try:
            chain_id, default_url, name = self.CHAIN_CONFIG[chain]
        except KeyError:
            raise RuntimeError("EVM chain is unsupported") from None
        suffix = name.upper()
        endpoint = (
            os.environ.get(f"NOEMA_EVM_RPC_URL_{suffix}")
            or os.environ.get("NOEMA_EVM_RPC_URL")
            or default_url
        )
        return chain_id, endpoint, name

    async def inspect(self) -> dict[str, Any]:
        return await self._run({"operation": "evm_inspect"})

    async def balances(self, chain: Chain, *, tokens: list[str] | None = None) -> dict[str, Any]:
        chain_id, rpc_url, name = self.chain_config(chain)
        del chain_id
        token_indexer_url = (
            os.environ.get("NOEMA_EVM_TOKEN_INDEXER_URL_BASE", "https://base.blockscout.com/api/v2")
            if chain is Chain.BASE else None
        )
        result = await self._run({
            "operation": "evm_balances",
            "chain": name,
            "expected_address": self.expected_address,
            "rpc_url": rpc_url,
            "tokens": tokens or [],
            "token_indexer_url": token_indexer_url,
        })
        if (
            token_indexer_url
            and result.get("token_status") == "unavailable"
            and isinstance(result.get("address"), str)
        ):
            indexed = await _read_evm_token_balances(token_indexer_url, result["address"])
            if indexed is not None:
                result["tokens"] = indexed
                result["token_status"] = "indexed"
        return result

    async def simulate(self, intent: WalletIntent) -> dict[str, Any]:
        return await self._run(self._intent_payload(intent, operation="evm_simulate_transaction"))

    async def sign_dry_run(self, intent: WalletIntent) -> dict[str, Any]:
        return await self._run(self._intent_payload(intent, operation="evm_sign_dry_run"))

    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        if not self.enabled or self.master_halt:
            raise RuntimeError("EVM signer disabled or owner master halt enabled")
        payload = self._intent_payload(intent, operation="evm_execute_transaction")
        payload.update({"signer_enabled": self.enabled, "master_halt": self.master_halt})
        result = await self._run(payload)
        return WalletExecutionReceipt(
            intent_id=intent.intent_id,
            provider_reference=f"evm-keychain-signer:{result.get('chain', 'unknown')}",
            chain=intent.chain,
            transaction_reference=result.get("transaction_hash"),
            submitted=result.get("status") in {"confirmed", "reverted", "submitted"},
            status=str(result.get("status", "unknown")),
            chain_id=result.get("chain_id"),
            block_number=result.get("block_number"),
            fee_amount_atomic=int(result["fee_paid_wei"]) if result.get("fee_paid_wei") is not None else None,
            post_balance_atomic=int(result["post_balance_wei"]) if result.get("post_balance_wei") is not None else None,
        )

    def _intent_payload(self, intent: WalletIntent, *, operation: str) -> dict[str, Any]:
        if intent.chain not in self.CHAIN_CONFIG:
            raise RuntimeError("EVM signer received an unsupported chain")
        if intent.action not in {"evm_native_transfer", "evm_erc20_transfer"}:
            raise RuntimeError("EVM signer received an unsupported structured action")
        if not intent.mission_id or not intent.evidence_ids:
            raise RuntimeError("EVM transfer requires a mission and evidence references")
        if not intent.destination or intent.amount_atomic is None or intent.fee_limit_wei is None:
            raise RuntimeError("EVM transfer intent is incomplete")
        if intent.operational_validation and (
            intent.chain is not Chain.BASE
            or intent.action != "evm_native_transfer"
            or intent.amount_atomic != 0
            or not self.expected_address
            or intent.destination.lower() != self.expected_address.lower()
        ):
            raise RuntimeError("operational validation is restricted to a zero-value Base self-call")
        _, rpc_url, name = self.chain_config(intent.chain)
        return {
            "operation": operation,
            "chain": name,
            "rpc_url": rpc_url,
            "expected_address": self.expected_address,
            "source": self.expected_address,
            "destination": intent.destination,
            "action": intent.action,
            "amount_wei": intent.amount_atomic if intent.action == "evm_native_transfer" else 0,
            "amount_atomic": intent.amount_atomic,
            "fee_limit_wei": intent.fee_limit_wei,
            "contract_or_program": intent.contract_or_program,
            "operational_validation": intent.operational_validation,
        }

    async def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(LocalSolanaWalletSigner._run_sync, self, request)


class LocalBitcoinWalletSigner:
    """Isolated WIF/private-key signer for the confirmed native-SegWit identity."""

    def __init__(
        self,
        *,
        expected_address: str | None = None,
        esplora_url: str | None = None,
        enabled: bool | None = None,
        master_halt: bool | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.expected_address = expected_address or os.environ.get("NOEMA_BITCOIN_ADDRESS")
        self.esplora_url = esplora_url or os.environ.get(
            "NOEMA_BITCOIN_ESPLORA_URL", "https://blockstream.info/api"
        )
        self.enabled = os.environ.get("NOEMA_BITCOIN_SIGNER_ENABLED") == "1" if enabled is None else enabled
        self.master_halt = (
            os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
            or os.environ.get("NOEMA_BITCOIN_MASTER_HALT", "1") != "0"
            if master_halt is None else master_halt
        )
        self.python_executable = python_executable or sys.executable

    async def inspect(self) -> dict[str, Any]:
        return await self._run({"operation": "bitcoin_inspect"})

    async def balance(self) -> dict[str, Any]:
        return await self._run({
            "operation": "bitcoin_balance",
            "expected_address": self.expected_address,
            "esplora_url": self.esplora_url,
        })

    async def simulate(self, intent: WalletIntent) -> dict[str, Any]:
        return await self._run(self._intent_payload(intent, operation="bitcoin_simulate_transaction"))

    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        if not self.enabled or self.master_halt:
            raise RuntimeError("Bitcoin signer disabled or owner master halt enabled")
        result = await self._run(self._intent_payload(intent, operation="bitcoin_execute_transaction") | {
            "signer_enabled": self.enabled,
            "master_halt": self.master_halt,
        })
        return WalletExecutionReceipt(
            intent_id=intent.intent_id,
            provider_reference="bitcoin-keychain-signer:mainnet",
            chain=Chain.BITCOIN,
            transaction_reference=result.get("transaction_hash"),
            submitted=result.get("status") in {"confirmed", "submitted"},
            status=str(result.get("status", "unknown")),
            fee_amount_atomic=result.get("fee_paid_sats"),
        )

    def _intent_payload(self, intent: WalletIntent, *, operation: str) -> dict[str, Any]:
        if intent.chain is not Chain.BITCOIN:
            raise RuntimeError("Bitcoin signer received a different chain")
        if intent.action != "bitcoin_send":
            raise RuntimeError("Bitcoin signer received an unsupported structured action")
        if not intent.mission_id or not intent.evidence_ids:
            raise RuntimeError("Bitcoin transfer requires a mission and evidence references")
        if not intent.destination or intent.amount_atomic is None or intent.fee_limit_sats is None:
            raise RuntimeError("Bitcoin transfer intent is incomplete")
        return {
            "operation": operation,
            "expected_address": self.expected_address,
            "destination": intent.destination,
            "amount_sats": intent.amount_atomic,
            "fee_limit_sats": intent.fee_limit_sats,
            "esplora_url": self.esplora_url,
        }

    async def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(LocalSolanaWalletSigner._run_sync, self, request)


class LocalOwnerWalletSigner:
    """Route one structured intent to its chain-specific isolated signer."""

    def __init__(self) -> None:
        self.signers: dict[Chain, WalletSigner] = {
            Chain.SOLANA: LocalSolanaWalletSigner(),
            Chain.ETHEREUM: LocalEvmWalletSigner(),
            Chain.BASE: LocalEvmWalletSigner(),
            Chain.POLYGON: LocalEvmWalletSigner(),
            Chain.BITCOIN: LocalBitcoinWalletSigner(),
        }

    def _signer(self, chain: Chain) -> WalletSigner:
        try:
            return self.signers[chain]
        except KeyError:
            raise RuntimeError("wallet chain is unsupported") from None

    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        return await self._signer(intent.chain).execute(intent)

    async def simulate(self, intent: WalletIntent) -> dict[str, Any]:
        signer = self._signer(intent.chain)
        simulate = getattr(signer, "simulate", None)
        if simulate is None:
            raise RuntimeError("chain simulation is unavailable")
        return await simulate(intent)
