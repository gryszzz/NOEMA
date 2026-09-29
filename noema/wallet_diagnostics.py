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
            "signer_process_enabled": os.environ.get("NOEMA_SOLANA_SIGNER_ENABLED") == "1",
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
            "signer_process_enabled": os.environ.get("NOEMA_EVM_SIGNER_ENABLED") == "1",
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
            "signer_process_enabled": os.environ.get("NOEMA_EVM_SIGNER_ENABLED") == "1",
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
            "signer_process_enabled": os.environ.get("NOEMA_EVM_SIGNER_ENABLED") == "1",
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
            "signer_process_enabled": os.environ.get("NOEMA_BITCOIN_SIGNER_ENABLED") == "1",
            "master_halt": (
                os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
                or os.environ.get("NOEMA_BITCOIN_MASTER_HALT", "1") != "0"
            ),
            "signer": "isolated macOS Keychain process",
        },
    ]
    for row in networks:
        row.update(project_wallet_capabilities(
            connected=None, authenticated=False, readable=None, funded=None,
            signer_configured=row["credential_present"],
            credentials_isolated=row["credential_present"],
            halted=row["master_halt"] or policy.master_halt,
        ))
        # Compatibility field means effective live permission, never an env toggle.
        row["signing_enabled"] = False
    return {
        "master_halt": policy.master_halt,
        "allowed_chains": sorted(chain.value for chain in policy.allowed_chains),
        "allowed_venues": sorted(policy.allowed_venues),
        "allowed_contracts_count": len(policy.allowed_contracts),
        "max_transaction_usd": (
            None if policy.max_transaction_usd is None else str(policy.max_transaction_usd)
        ),
        "max_daily_notional_usd": (
            None if policy.max_daily_notional_usd is None else str(policy.max_daily_notional_usd)
        ),
        "max_slippage_bps": str(policy.max_slippage_bps),
        "minimum_reserve_usd": str(policy.minimum_reserve_usd),
        "require_evidence": policy.require_evidence,
        "signer_state": (
            "configured_execution_disabled"
            if any(row["credential_present"] for row in networks) else "unconfigured"
        ),
        "credential_present": any((
            solana_signing_credential_present(),
            evm_signing_credential_present(),
            bitcoin_signing_credential_present(),
        )),
        "credential_store": "macOS Keychain",
        "signing_enabled": False,
        "live_execution_enabled": False,
        "coordinator_wired": False,
        "mission_authority_present": False,
        "authority_status": "no_mission_authority_configured",
        "authority_source": "separate owner-controlled authority registry; mission/research data cannot grant",
        "delegated_capital_authority": False,
        "dedicated_capital_pool_configured": policy.dedicated_capital_authority,
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
                         "signer_process_enabled": signer.enabled, "master_halt": signer.master_halt})
        except Exception:  # noqa: BLE001 - public status must not expose provider errors
            rows.append({"chain": "solana", "network": "mainnet-beta", "address": signer.expected_address,
                         "status": "unavailable", "signer_process_enabled": signer.enabled,
                         "master_halt": signer.master_halt})
    evm_present = evm_signing_credential_present()
    if evm_present:
        signer = LocalEvmWalletSigner()
        for chain in (Chain.ETHEREUM, Chain.BASE, Chain.POLYGON):
            try:
                balance = await signer.balances(chain)
                rows.append({"chain": chain.value, "network": "mainnet", **balance,
                             "signer_process_enabled": signer.enabled, "master_halt": signer.master_halt})
            except Exception:  # noqa: BLE001 - public status must not expose RPC details
                chain_id, _url, _name = signer.chain_config(chain)
                rows.append({"chain": chain.value, "network": "mainnet", "chain_id": chain_id,
                             "address": signer.expected_address, "status": "unavailable",
                             "signer_process_enabled": signer.enabled, "master_halt": signer.master_halt})
    bitcoin_present = bitcoin_signing_credential_present()
    if bitcoin_present:
        try:
            signer = LocalBitcoinWalletSigner()
            balance = await signer.balance()
            rows.append({"chain": "bitcoin", "network": "mainnet", **balance,
                         "signer_process_enabled": signer.enabled, "master_halt": signer.master_halt})
        except Exception:  # noqa: BLE001 - public status must not expose provider errors
            rows.append({"chain": "bitcoin", "network": "mainnet", "status": "unavailable",
                         "signer_process_enabled": False, "master_halt": True})
    credentials = {
        "solana": solana_present,
        "ethereum": evm_present,
        "base": evm_present,
        "polygon": evm_present,
        "bitcoin": bitcoin_present,
    }
    for row in rows:
        chain = str(row.get("chain", ""))
        readable = str(row.get("status", "")).startswith("read_only")
        funded = _wallet_row_funded(row)
        row.update(project_wallet_capabilities(
            connected=readable, authenticated=False, readable=readable, funded=funded,
            signer_configured=credentials.get(chain, False),
            credentials_isolated=credentials.get(chain, False),
            halted=bool(row.get("master_halt", True)), research_enabled=readable,
        ))
        row["signing_enabled"] = False
    return rows


def _wallet_row_funded(row: dict[str, Any]) -> bool | None:
    """Return false/true only when an actual balance field was read."""
    keys = ("sol", "native_balance", "native_balance_wei", "btc", "total_sats")
    values = [row[key] for key in keys if key in row and row[key] is not None]
    values.extend(
        token.get("raw_amount") for token in row.get("tokens", [])
        if isinstance(token, dict) and token.get("raw_amount") is not None
    )
    if not values:
        return None
    try:
        return any(Decimal(str(value)) > 0 for value in values)
    except Exception:  # noqa: BLE001 - malformed balances stay unknown
        return None


def project_wallet_capabilities(
    *, connected: bool | None, authenticated: bool | None, readable: bool | None,
    funded: bool | None, signer_configured: bool, credentials_isolated: bool,
    halted: bool,
    research_enabled: bool | None = None,
) -> dict[str, bool | None]:
    """Canonical secret-free capability states shared by Home and diagnostics."""
    return {
        "connected": connected,
        "authenticated": authenticated,
        "readable": readable,
        "funded": funded,
        "signer_configured": signer_configured,
        "credentials_isolated": credentials_isolated,
        "research_enabled": research_enabled,
        "paper_enabled": False,
        "mission_authority_present": False,
        "live_execution_enabled": False,
        "halted": halted,
        "coordinator_wired": False,
    }
