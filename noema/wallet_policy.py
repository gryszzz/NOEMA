from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .wallet_intents import WalletIntent
from .wallet_types import Chain


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


@dataclass(frozen=True)
class AgentWalletState:
    estimated_wallet_value_usd: Decimal
    available_cash_like_usd: Decimal
    daily_notional_used_usd: Decimal


@dataclass(frozen=True)
class WalletPolicyDecision:
    approved: bool
    reasons: tuple[str, ...]


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

    if policy.allowed_venues and intent.venue not in policy.allowed_venues:
        reasons.append(f"venue not allowed: {intent.venue}")

    if (
        intent.contract_or_program is not None
        and policy.allowed_contracts
        and intent.contract_or_program not in policy.allowed_contracts
    ):
        reasons.append("contract/program not allowlisted")

    if intent.notional_usd <= 0:
        reasons.append("notional must be positive")
    elif intent.notional_usd > policy.max_transaction_usd:
        reasons.append("per-transaction notional cap exceeded")

    projected_daily = state.daily_notional_used_usd + max(intent.notional_usd, Decimal(0))
    if projected_daily > policy.max_daily_notional_usd:
        reasons.append("daily notional cap exceeded")

    if intent.expected_slippage_bps < 0:
        reasons.append("slippage estimate cannot be negative")
    elif intent.expected_slippage_bps > policy.max_slippage_bps:
        reasons.append("slippage ceiling exceeded")

    post_trade_reserve = state.available_cash_like_usd - max(intent.notional_usd, Decimal(0))
    if post_trade_reserve < policy.minimum_reserve_usd:
        reasons.append("minimum reserve would be violated")

    if policy.require_evidence and not intent.evidence_ids:
        reasons.append("intent has no evidence references")

    return WalletPolicyDecision(approved=not reasons, reasons=tuple(reasons))
