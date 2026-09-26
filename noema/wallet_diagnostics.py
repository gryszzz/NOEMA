from __future__ import annotations

from decimal import Decimal
from typing import Any

from .wallet_policy import AgentWalletPolicy


def public_wallet_policy(policy: AgentWalletPolicy | None = None) -> dict[str, Any]:
    policy = policy or AgentWalletPolicy()
    return {
        "master_halt": policy.master_halt,
        "allowed_chains": sorted(chain.value for chain in policy.allowed_chains),
        "allowed_venues": sorted(policy.allowed_venues),
        "allowed_contracts_count": len(policy.allowed_contracts),
        "max_transaction_usd": str(policy.max_transaction_usd),
        "max_daily_notional_usd": str(policy.max_daily_notional_usd),
        "max_slippage_bps": str(policy.max_slippage_bps),
        "minimum_reserve_usd": str(policy.minimum_reserve_usd),
        "require_evidence": policy.require_evidence,
        "signer_state": "disabled",
    }


def decimal_or_zero(value: object | None) -> Decimal:
    return Decimal(0) if value is None else Decimal(str(value))
