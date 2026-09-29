from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from .wallet_credentials import (
    bitcoin_signing_credential_present,
    evm_signing_credential_present,
    solana_signing_credential_present,
)
from .wallet_policy import AgentWalletPolicy, dedicated_wallet_policy
from .wallet_types import Chain


def public_wallet_policy(policy: AgentWalletPolicy | None = None) -> dict[str, Any]:
    policy = policy or dedicated_wallet_policy()
    networks = [
        {
            "chain": "solana",
            "network": "mainnet-beta",
            "address": os.environ.get("NOEMA_SOLANA_WALLET_ADDRESS"),
            "credential_present": solana_signing_credential_present(),
            "signing_enabled": os.environ.get("NOEMA_SOLANA_SIGNER_ENABLED") == "1",
            "master_halt": (
                os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
                or os.environ.get("NOEMA_SOLANA_MASTER_HALT", "1") != "0"
            ),
            "signer": "isolated macOS Keychain process",
        },
        {
            "chain": "ethereum",
            "network": "mainnet",
            "chain_id": 1,
            "address": os.environ.get("NOEMA_EVM_ADDRESS"),
            "credential_present": evm_signing_credential_present(),
            "signing_enabled": os.environ.get("NOEMA_EVM_SIGNER_ENABLED") == "1",
            "master_halt": (
                os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
                or os.environ.get("NOEMA_EVM_MASTER_HALT", "1") != "0"
            ),
            "signer": "isolated macOS Keychain process",
        },
        {
            "chain": "base",
            "network": "mainnet",
            "chain_id": 8453,
            "address": os.environ.get("NOEMA_EVM_ADDRESS"),
            "credential_present": evm_signing_credential_present(),
            "signing_enabled": os.environ.get("NOEMA_EVM_SIGNER_ENABLED") == "1",
            "master_halt": (
                os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
                or os.environ.get("NOEMA_EVM_MASTER_HALT", "1") != "0"
            ),
            "signer": "isolated macOS Keychain process",
        },
        {
            "chain": "polygon",
            "network": "mainnet",
            "chain_id": 137,
            "address": os.environ.get("NOEMA_EVM_ADDRESS"),
            "credential_present": evm_signing_credential_present(),
            "signing_enabled": os.environ.get("NOEMA_EVM_SIGNER_ENABLED") == "1",
            "master_halt": (
                os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
                or os.environ.get("NOEMA_EVM_MASTER_HALT", "1") != "0"
            ),
            "signer": "isolated macOS Keychain process",
        },
        {
            "chain": "bitcoin",
            "network": "mainnet",
            "address": os.environ.get("NOEMA_BITCOIN_ADDRESS"),
            "credential_present": bitcoin_signing_credential_present(),
            "signing_enabled": os.environ.get("NOEMA_BITCOIN_SIGNER_ENABLED") == "1",
            "master_halt": (
                os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
                or os.environ.get("NOEMA_BITCOIN_MASTER_HALT", "1") != "0"
            ),
            "signer": "isolated macOS Keychain process",
        },
    ]
    return {
        "master_halt": policy.master_halt,
        "allowed_chains": sorted(chain.value for chain in policy.allowed_chains),
        "allowed_venues": sorted(policy.allowed_venues),
        "allowed_contracts_count": len(policy.allowed_contracts),
        "max_transaction_usd": (
            None if policy.dedicated_capital_authority else str(policy.max_transaction_usd)
        ),
        "max_daily_notional_usd": (
            None if policy.dedicated_capital_authority else str(policy.max_daily_notional_usd)
        ),
        "max_slippage_bps": str(policy.max_slippage_bps),
        "minimum_reserve_usd": str(policy.minimum_reserve_usd),
        "require_evidence": policy.require_evidence,
        "signer_state": "disabled",
        "credential_present": any((
            solana_signing_credential_present(),
            evm_signing_credential_present(),
            bitcoin_signing_credential_present(),
        )),
        "credential_store": "macOS Keychain",
        "signing_enabled": any(
            row["signing_enabled"] and not row["master_halt"] and row["credential_present"]
            for row in networks
        ),
        "delegated_capital_authority": policy.dedicated_capital_authority,
        "wallet_networks": networks,
        "raw_credentials_exposed": False,
    }


def decimal_or_zero(value: object | None) -> Decimal:
    return Decimal(0) if value is None else Decimal(str(value))


async def live_wallet_networks() -> list[dict[str, Any]]:
    """Read public balances through short-lived signer-boundary child processes."""
    from .wallet_signer import (
        LocalBitcoinWalletSigner,
        LocalEvmWalletSigner,
        LocalSolanaWalletSigner,
    )

    rows: list[dict[str, Any]] = []
    solana_present = solana_signing_credential_present()
    if solana_present:
        signer = LocalSolanaWalletSigner()
        try:
            balance = await signer.balances()
            rows.append({"chain": "solana", "network": "mainnet-beta", **balance,
                         "signing_enabled": signer.enabled, "master_halt": signer.master_halt})
        except Exception:  # noqa: BLE001 - public status must not expose provider errors
            rows.append({"chain": "solana", "network": "mainnet-beta", "address": signer.expected_address,
                         "status": "unavailable", "signing_enabled": signer.enabled,
                         "master_halt": signer.master_halt})
    evm_present = evm_signing_credential_present()
    if evm_present:
        signer = LocalEvmWalletSigner()
        for chain in (Chain.ETHEREUM, Chain.BASE, Chain.POLYGON):
            try:
                balance = await signer.balances(chain)
                rows.append({"chain": chain.value, "network": "mainnet", **balance,
                             "signing_enabled": signer.enabled, "master_halt": signer.master_halt})
            except Exception:  # noqa: BLE001 - public status must not expose RPC details
                chain_id, _url, _name = signer.chain_config(chain)
                rows.append({"chain": chain.value, "network": "mainnet", "chain_id": chain_id,
                             "address": signer.expected_address, "status": "unavailable",
                             "signing_enabled": signer.enabled, "master_halt": signer.master_halt})
    bitcoin_present = bitcoin_signing_credential_present()
    if bitcoin_present:
        try:
            signer = LocalBitcoinWalletSigner()
            balance = await signer.balance()
            rows.append({"chain": "bitcoin", "network": "mainnet", **balance,
                         "signing_enabled": signer.enabled, "master_halt": signer.master_halt})
        except Exception:  # noqa: BLE001 - public status must not expose provider errors
            rows.append({"chain": "bitcoin", "network": "mainnet", "status": "unavailable",
                         "signing_enabled": False, "master_halt": True})
    return rows
