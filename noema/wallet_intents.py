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


@dataclass(frozen=True)
class WalletExecutionReceipt:
    intent_id: str
    provider_reference: str
    chain: Chain
    transaction_reference: str | None
    submitted: bool
