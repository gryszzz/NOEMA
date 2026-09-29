from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

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
    max_transaction_usd: Decimal | None = Decimal(25)
    max_daily_notional_usd: Decimal | None = Decimal(100)
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


@dataclass(frozen=True)
class FinancialMissionAuthority:
    """Explicit owner-controlled authority, separate from mission/prompt data."""

    authority_id: str
    mission_id: str
    wallet_id: str
    allowed_chains: frozenset[Chain]
    allowed_networks: frozenset[str]
    allowed_actions: frozenset[str]
    allowed_venues: frozenset[str]
    allowed_protocols: frozenset[str]
    allowed_assets: frozenset[str]
    per_action_limit_usd: Decimal | None
    per_mission_limit_usd: Decimal | None
    daily_limit_usd: Decimal | None
    maximum_exposure_usd: Decimal | None
    expires_at: datetime
    revoked: bool = False
    capital_pool_id: str | None = None


def _positive_finite(value: Decimal | None) -> bool:
    try:
        return value is not None and value.is_finite() and value > 0
    except (AttributeError, InvalidOperation, TypeError):
        return False


def _finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


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
        master_halt=(
            os.environ.get("NOEMA_WALLET_MASTER_HALT", "1") != "0"
            or os.environ.get("NOEMA_MASTER_HALT", "0") == "1"
        ),
        dedicated_capital_authority=True,
    )


def evaluate_wallet_intent(
    intent: WalletIntent,
    state: AgentWalletState,
    policy: AgentWalletPolicy,
    *,
    authority: FinancialMissionAuthority | None = None,
    mission_status: str | None = None,
    intent_consumed: bool = False,
    mission_exposure_usd: Decimal | None = None,
    daily_exposure_usd: Decimal | None = None,
    live_execution_enabled: bool = False,
    signer_isolated: bool = False,
    state_is_current: bool = False,
    now: datetime | None = None,
) -> WalletPolicyDecision:
    reasons: list[str] = []
    now = now or datetime.now(UTC)

    if policy.master_halt:
        reasons.append("agent wallet master halt enabled")

    if intent.chain not in policy.allowed_chains:
        reasons.append(f"chain not allowed: {intent.chain.value}")

    # No mission, prompt, research result, or specialist output can create this
    # separately provisioned authority object.
    if authority is None:
        reasons.append("explicit financial mission authority is absent")
    if not mission_status:
        reasons.append("mission does not exist")
    elif mission_status != "running":
        reasons.append("mission is not currently active")
    if authority is not None:
        if not isinstance(authority.authority_id, str) or not authority.authority_id.strip():
            reasons.append("financial authority has no stable identity")
        if authority.revoked:
            reasons.append("mission financial authority is revoked")
        if authority.mission_id != intent.mission_id:
            reasons.append("authority does not match mission")
        if authority.wallet_id != (intent.wallet_id or ""):
            reasons.append("authority does not match wallet")
        if intent.chain not in authority.allowed_chains:
            reasons.append("authority does not include this chain")
        if not intent.network or intent.network not in authority.allowed_networks:
            reasons.append("authority does not include this network")
        if intent.action not in authority.allowed_actions:
            reasons.append("authority does not include this action")
        if intent.venue not in authority.allowed_venues:
            reasons.append("authority does not include this venue")
        if not intent.contract_or_program or intent.contract_or_program not in authority.allowed_protocols:
            reasons.append("authority does not include this protocol")
        if not {intent.asset_in, intent.asset_out} <= authority.allowed_assets:
            reasons.append("authority does not include these assets")
        for label, cap in (
            ("per-action", authority.per_action_limit_usd),
            ("per-mission", authority.per_mission_limit_usd),
            ("daily", authority.daily_limit_usd),
            ("maximum exposure", authority.maximum_exposure_usd),
        ):
            if not _positive_finite(cap):
                reasons.append(f"authority {label} cap is missing or nonpositive")
        if (not isinstance(authority.expires_at, datetime)
                or authority.expires_at.tzinfo is None or authority.expires_at <= now):
            reasons.append("mission financial authority is expired")

    if (not isinstance(intent.expires_at, datetime)
            or intent.expires_at.tzinfo is None or intent.expires_at <= now):
        reasons.append("intent is expired or has no valid expiry")
    if not isinstance(intent.intent_id, str) or not intent.intent_id.strip():
        reasons.append("intent has no stable identity")
    if intent_consumed:
        reasons.append("intent has already been consumed")
    if not signer_isolated:
        reasons.append("signing capability is not isolated from reasoning context")
    if not state_is_current:
        reasons.append("current wallet and exposure state is unavailable")
    if not _finite_decimal(mission_exposure_usd) or mission_exposure_usd < 0:
        reasons.append("mission cumulative exposure is unavailable or invalid")
    if not _finite_decimal(daily_exposure_usd) or (daily_exposure_usd is not None and daily_exposure_usd < 0):
        reasons.append("daily cumulative exposure is unavailable or invalid")
    if not live_execution_enabled:
        reasons.append("live execution is disabled")

    if policy.allowed_venues and intent.venue not in policy.allowed_venues:
        reasons.append(f"venue not allowed: {intent.venue}")

    if (
        intent.contract_or_program is not None
        and policy.allowed_contracts
        and intent.contract_or_program not in policy.allowed_contracts
    ):
        reasons.append("contract/program not allowlisted")

    if not _finite_decimal(intent.notional_usd) or intent.notional_usd <= 0:
        reasons.append("notional must be positive")
    elif not _positive_finite(policy.max_transaction_usd):
        reasons.append("global per-transaction cap is missing or nonpositive")
    elif intent.notional_usd > policy.max_transaction_usd:
        reasons.append("per-transaction notional cap exceeded")

    state_daily = state.daily_notional_used_usd
    if not _finite_decimal(state_daily) or state_daily < 0:
        reasons.append("daily wallet exposure state is invalid")
        state_daily = Decimal(0)
    notional = intent.notional_usd if _finite_decimal(intent.notional_usd) else Decimal(0)
    projected_daily = state_daily + max(notional, Decimal(0))
    if not _positive_finite(policy.max_daily_notional_usd):
        reasons.append("global daily notional cap is missing or nonpositive")
    elif projected_daily > policy.max_daily_notional_usd:
        reasons.append("daily notional cap exceeded")

    if not _finite_decimal(intent.expected_slippage_bps) or intent.expected_slippage_bps < 0:
        reasons.append("slippage estimate cannot be negative")
    elif not _finite_decimal(policy.max_slippage_bps) or policy.max_slippage_bps < 0:
        reasons.append("global slippage cap is missing or invalid")
    elif (
        intent.expected_slippage_bps > policy.max_slippage_bps
    ):
        reasons.append("slippage ceiling exceeded")

    cash = state.available_cash_like_usd
    if not _finite_decimal(cash) or cash < 0:
        reasons.append("available wallet balance state is invalid")
        cash = Decimal(0)
    reserve = policy.minimum_reserve_usd
    if not _finite_decimal(reserve) or reserve < 0:
        reasons.append("minimum wallet reserve is missing or invalid")
        reserve = Decimal(0)
    post_trade_reserve = cash - max(notional, Decimal(0))
    if post_trade_reserve < reserve:
        reasons.append("minimum reserve would be violated")

    if policy.require_evidence and not intent.evidence_ids:
        reasons.append("intent has no evidence references")

    if intent.action not in SUPPORTED_WALLET_ACTIONS.get(intent.chain, frozenset()):
        reasons.append("wallet action is not supported on this chain")
    if (isinstance(intent.amount_atomic, bool) or not isinstance(intent.amount_atomic, int)
            or intent.amount_atomic <= 0):
        reasons.append("atomic asset amount must be a positive integer")

    if not intent.mission_id:
        reasons.append("intent has no mission reference")

    if authority is not None:
        if notional > (authority.per_action_limit_usd or Decimal(0)):
            reasons.append("mission per-action cap exceeded")
        mission_used = mission_exposure_usd if _finite_decimal(mission_exposure_usd) else Decimal(0)
        if mission_used + notional > (authority.per_mission_limit_usd or Decimal(0)):
            reasons.append("mission cumulative cap exceeded")
        if (_finite_decimal(daily_exposure_usd) and
                daily_exposure_usd + notional > (authority.daily_limit_usd or Decimal(0))):
            reasons.append("mission daily cap exceeded")
        if mission_used + notional > (authority.maximum_exposure_usd or Decimal(0)):
            reasons.append("mission maximum exposure exceeded")

    if intent.chain is Chain.SOLANA and (
        isinstance(intent.fee_limit_lamports, bool)
        or not isinstance(intent.fee_limit_lamports, int)
        or intent.fee_limit_lamports < 0
    ):
        reasons.append("Solana intent has no valid fee ceiling")
    if intent.chain in {Chain.ETHEREUM, Chain.BASE, Chain.POLYGON} and (
        isinstance(intent.fee_limit_wei, bool)
        or not isinstance(intent.fee_limit_wei, int)
        or intent.fee_limit_wei < 0
    ):
        reasons.append("EVM intent has no valid fee ceiling")

    return WalletPolicyDecision(approved=not reasons, reasons=tuple(reasons))
