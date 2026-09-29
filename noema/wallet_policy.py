from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from .wallet_intents import WalletIntent
from .wallet_types import Chain

SUPPORTED_WALLET_ACTIONS = {
    Chain.SOLANA: frozenset({"sol_transfer"}),
    Chain.ETHEREUM: frozenset({"evm_native_transfer", "evm_erc20_transfer"}),
    Chain.BASE: frozenset({"evm_native_transfer", "evm_erc20_transfer"}),
    Chain.POLYGON: frozenset({"evm_native_transfer", "evm_erc20_transfer"}),
    Chain.BITCOIN: frozenset({"bitcoin_send"}),
}


@dataclass(frozen=True)
class AgentWalletPolicy:
    allowed_chains: frozenset[Chain] = frozenset({Chain.SOLANA, Chain.BASE})
    allowed_venues: frozenset[str] = frozenset()
    allowed_contracts: frozenset[str] = frozenset()
    max_transaction_usd: Decimal = Decimal(25)
    max_daily_notional_usd: Decimal = Decimal(100)
    max_slippage_bps: Decimal = Decimal(75)
    minimum_reserve_usd: Decimal = Decimal(100)
    require_evidence: bool = True
    master_halt: bool = True
    dedicated_capital_authority: bool = False


@dataclass(frozen=True)
class AgentWalletState:
    estimated_wallet_value_usd: Decimal
    available_cash_like_usd: Decimal
    daily_notional_used_usd: Decimal


@dataclass(frozen=True)
class WalletPolicyDecision:
    approved: bool
    reasons: tuple[str, ...]


def dedicated_wallet_policy() -> AgentWalletPolicy:
    """Policy for explicitly owner-funded isolated wallet identities."""
    return AgentWalletPolicy(
        allowed_chains=frozenset({
            Chain.SOLANA, Chain.ETHEREUM, Chain.BASE, Chain.POLYGON, Chain.BITCOIN,
        }),
        allowed_venues=frozenset({
            "solana-mainnet", "ethereum-mainnet", "base-mainnet", "polygon-mainnet",
            "bitcoin-mainnet",
        }),
        max_transaction_usd=Decimal(0),
        max_daily_notional_usd=Decimal(0),
        max_slippage_bps=Decimal(0),
        minimum_reserve_usd=Decimal(0),
        require_evidence=True,
        master_halt=os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0",
        dedicated_capital_authority=True,
    )


def evaluate_wallet_intent(
    intent: WalletIntent,
    state: AgentWalletState,
    policy: AgentWalletPolicy,
) -> WalletPolicyDecision:
    reasons: list[str] = []

    if policy.master_halt:
        reasons.append("agent wallet master halt enabled")

    if intent.chain not in policy.allowed_chains:
        reasons.append(f"chain not allowed: {intent.chain.value}")

    if policy.dedicated_capital_authority:
        if intent.action not in SUPPORTED_WALLET_ACTIONS.get(intent.chain, frozenset()):
            reasons.append("wallet action is not supported on this chain")
        zero_value_validation = (
            intent.operational_validation
            and intent.chain is Chain.BASE
            and intent.action == "evm_native_transfer"
            and intent.amount_atomic == 0
            and intent.destination is not None
            and intent.destination.lower() == os.environ.get("NOEMA_EVM_ADDRESS", "").lower()
        )
        if intent.operational_validation and not zero_value_validation:
            reasons.append("operational validation is restricted to a zero-value Base self-call")
        if (
            isinstance(intent.amount_atomic, bool)
            or not isinstance(intent.amount_atomic, int)
            or (intent.amount_atomic <= 0 and not zero_value_validation)
        ):
            reasons.append("atomic asset amount must be a positive integer")

    if policy.allowed_venues and intent.venue not in policy.allowed_venues:
        reasons.append(f"venue not allowed: {intent.venue}")

    if (
        intent.contract_or_program is not None
        and policy.allowed_contracts
        and intent.contract_or_program not in policy.allowed_contracts
    ):
        reasons.append("contract/program not allowlisted")

    zero_value_validation = (
        intent.operational_validation
        and intent.chain is Chain.BASE
        and intent.action == "evm_native_transfer"
        and intent.amount_atomic == 0
    )
    if not intent.notional_usd.is_finite() or (intent.notional_usd <= 0 and not zero_value_validation):
        reasons.append("notional must be positive")
    elif (
        not policy.dedicated_capital_authority
        and intent.notional_usd > policy.max_transaction_usd
    ):
        reasons.append("per-transaction notional cap exceeded")

    projected_daily = state.daily_notional_used_usd + max(intent.notional_usd, Decimal(0))
    if not policy.dedicated_capital_authority and projected_daily > policy.max_daily_notional_usd:
        reasons.append("daily notional cap exceeded")

    if not intent.expected_slippage_bps.is_finite() or intent.expected_slippage_bps < 0:
        reasons.append("slippage estimate cannot be negative")
    elif (
        not policy.dedicated_capital_authority
        and intent.expected_slippage_bps > policy.max_slippage_bps
    ):
        reasons.append("slippage ceiling exceeded")

    post_trade_reserve = state.available_cash_like_usd - max(intent.notional_usd, Decimal(0))
    if not policy.dedicated_capital_authority and post_trade_reserve < policy.minimum_reserve_usd:
        reasons.append("minimum reserve would be violated")

    if policy.require_evidence and not intent.evidence_ids:
        reasons.append("intent has no evidence references")

    if policy.dedicated_capital_authority and not intent.mission_id:
        reasons.append("intent has no mission reference")

    if policy.dedicated_capital_authority and intent.chain is Chain.SOLANA and (
        isinstance(intent.fee_limit_lamports, bool)
        or not isinstance(intent.fee_limit_lamports, int)
        or intent.fee_limit_lamports < 0
    ):
        reasons.append("Solana intent has no valid fee ceiling")
    if policy.dedicated_capital_authority and intent.chain in {Chain.ETHEREUM, Chain.BASE, Chain.POLYGON} and (
        isinstance(intent.fee_limit_wei, bool)
        or not isinstance(intent.fee_limit_wei, int)
        or intent.fee_limit_wei < 0
    ):
        reasons.append("EVM intent has no valid fee ceiling")

    return WalletPolicyDecision(approved=not reasons, reasons=tuple(reasons))
