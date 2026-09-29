from decimal import Decimal

import pytest

from noema.agent_wallet import AgentWallet
from noema.economic_ledger import EconomicLedger
from noema.mission_store import MissionStore
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


class ConfirmingSigner:
    async def execute(self, intent: WalletIntent) -> WalletExecutionReceipt:
        return WalletExecutionReceipt(
            intent_id=intent.intent_id,
            provider_reference="local-signer",
            chain=intent.chain,
            transaction_reference="confirmed-hash",
            submitted=True,
            status="confirmed",
            fee_lamports=5_000,
            slot=99,
            pre_balance_lamports=10_000,
            post_balance_lamports=0,
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


@pytest.mark.asyncio
async def test_confirmed_wallet_receipt_reconciles_to_economic_and_mission_ledgers(tmp_path) -> None:
    db_path = str(tmp_path / "noema.db")
    missions = MissionStore(db_path)
    mission_id = missions.discover(
        trial_id="trial-1", evidence_hash="evidence-1", objective="Bounded wallet experiment",
        specialist="NOEMA",
    )
    policy = AgentWalletPolicy(
        allowed_chains=frozenset({Chain.SOLANA}),
        allowed_venues=frozenset({"jupiter"}),
        max_transaction_usd=Decimal(25),
        max_daily_notional_usd=Decimal(100),
        minimum_reserve_usd=Decimal(0),
        require_evidence=True,
        master_halt=False,
    )
    budget = WalletBudgetLedger(db_path)
    wallet = AgentWallet(
        wallet_id="agent",
        signer=ConfirmingSigner(),
        policy=policy,
        budget=budget,
        economic_ledger=EconomicLedger(db_path),
        mission_store=missions,
    )
    result = await wallet.process(
        WalletIntent(
            intent_id="intent-confirmed",
            chain=Chain.SOLANA,
            venue="jupiter",
            action="swap",
            asset_in="USDC",
            asset_out="SOL",
            notional_usd=Decimal(10),
            expected_slippage_bps=Decimal(20),
            evidence_ids=("evidence-1",),
            mission_id=mission_id,
        ),
        AgentWalletState(
            estimated_wallet_value_usd=Decimal(500),
            available_cash_like_usd=Decimal(300),
            daily_notional_used_usd=Decimal(0),
        ),
    )
    assert result.receipt is not None and result.receipt.status == "confirmed"
    event = wallet.economic_ledger.conn.execute(
        "SELECT event_type,amount_usd,payload_json FROM economic_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event[0] == "wallet_transaction_confirmed"
    assert event[1] is None  # a transfer is not booked as revenue or USD P&L
    assert "confirmed-hash" in event[2]
    mission = missions.conn.execute(
        "SELECT result_json FROM missions WHERE mission_id=?", (mission_id,)
    ).fetchone()
    assert '"wallet_execution"' in mission[0]
    mission_event = missions.conn.execute(
        "SELECT event_type,status FROM mission_events WHERE mission_id=? ORDER BY id DESC LIMIT 1",
        (mission_id,),
    ).fetchone()
    assert tuple(mission_event) == ("wallet_execution_confirmed", "confirmed")
    assert budget.daily_notional("agent") == Decimal(10)
