from decimal import Decimal

import pytest

from noema.agent_wallet import AgentWallet
from noema.wallet_budget import WalletBudgetLedger
from noema.wallet_intents import WalletExecutionReceipt, WalletIntent
from noema.wallet_policy import AgentWalletPolicy, AgentWalletState
from noema.wallet_types import Chain


class FakeSigner:
    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        return WalletExecutionReceipt(
            intent_id=intent.intent_id,
            provider_reference="provider-1",
            chain=intent.chain,
            transaction_reference="tx-1",
            submitted=True,
        )


def approved_policy() -> AgentWalletPolicy:
    return AgentWalletPolicy(
        allowed_chains=frozenset({Chain.SOLANA}),
        allowed_venues=frozenset({"jupiter"}),
        allowed_contracts=frozenset({"program"}),
        max_transaction_usd=Decimal(25),
        max_daily_notional_usd=Decimal(100),
        max_slippage_bps=Decimal(50),
        minimum_reserve_usd=Decimal(100),
        require_evidence=True,
        master_halt=False,
    )


@pytest.mark.asyncio
async def test_agent_wallet_calls_signer_only_after_policy_passes(tmp_path) -> None:
    wallet = AgentWallet(
        wallet_id="agent",
        signer=FakeSigner(),
        policy=approved_policy(),
        budget=WalletBudgetLedger(str(tmp_path / "noema.db")),
    )
    result = await wallet.process(
        WalletIntent(
            intent_id="i1",
            chain=Chain.SOLANA,
            venue="jupiter",
            action="swap",
            asset_in="USDC",
            asset_out="SOL",
            notional_usd=Decimal(10),
            expected_slippage_bps=Decimal(20),
            contract_or_program="program",
            evidence_ids=("e1",),
        ),
        AgentWalletState(
            estimated_wallet_value_usd=Decimal(500),
            available_cash_like_usd=Decimal(300),
            daily_notional_used_usd=Decimal(0),
        ),
    )
    assert result.decision.approved is True
    assert result.receipt is not None
    assert result.receipt.submitted is True


@pytest.mark.asyncio
async def test_agent_wallet_never_calls_signer_when_halted(tmp_path) -> None:
    wallet = AgentWallet(
        wallet_id="agent",
        signer=FakeSigner(),
        policy=AgentWalletPolicy(),
        budget=WalletBudgetLedger(str(tmp_path / "noema.db")),
    )
    result = await wallet.process(
        WalletIntent(
            intent_id="i2",
            chain=Chain.SOLANA,
            venue="jupiter",
            action="swap",
            asset_in="USDC",
            asset_out="SOL",
            notional_usd=Decimal(10),
            expected_slippage_bps=Decimal(20),
            evidence_ids=("e1",),
        ),
        AgentWalletState(
            estimated_wallet_value_usd=Decimal(500),
            available_cash_like_usd=Decimal(300),
            daily_notional_used_usd=Decimal(0),
        ),
    )
    assert result.decision.approved is False
    assert result.receipt is None
