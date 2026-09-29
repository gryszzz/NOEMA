from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .wallet_types import Chain


@dataclass(frozen=True)
class WalletIntent:
    intent_id: str
    chain: Chain
    venue: str
    action: str
    asset_in: str
    asset_out: str
    notional_usd: Decimal
    expected_slippage_bps: Decimal
    destination: str | None = None
    contract_or_program: str | None = None
    strategy_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    mission_id: str | None = None
    amount_atomic: int | None = None
    expected_output_atomic: int | None = None
    fee_limit_lamports: int | None = None
    minimum_output_atomic: int | None = None
    fee_limit_wei: int | None = None
    fee_limit_sats: int | None = None
    operational_validation: bool = False


@dataclass(frozen=True)
class WalletExecutionReceipt:
    intent_id: str
    provider_reference: str
    chain: Chain
    transaction_reference: str | None
    submitted: bool
    status: str = "submitted"
    fee_lamports: int | None = None
    slot: int | None = None
    pre_balance_lamports: int | None = None
    post_balance_lamports: int | None = None
    chain_id: int | None = None
    block_number: int | None = None
    fee_amount_atomic: int | None = None
    post_balance_atomic: int | None = None
