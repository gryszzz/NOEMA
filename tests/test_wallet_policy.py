from decimal import Decimal

from noema.wallet_intents import WalletIntent
from noema.wallet_policy import (
    AgentWalletPolicy,
    AgentWalletState,
    dedicated_wallet_policy,
    evaluate_wallet_intent,
)
from noema.wallet_types import Chain


def intent(**overrides) -> WalletIntent:
    values = {
        "intent_id": "i1",
        "chain": Chain.SOLANA,
        "venue": "jupiter",
        "action": "swap",
        "asset_in": "USDC",
        "asset_out": "SOL",
        "notional_usd": Decimal(10),
        "expected_slippage_bps": Decimal(20),
        "contract_or_program": "jupiter-program",
        "evidence_ids": ("e1",),
    }
    values.update(overrides)
    return WalletIntent(**values)


def state() -> AgentWalletState:
    return AgentWalletState(
        estimated_wallet_value_usd=Decimal(500),
        available_cash_like_usd=Decimal(300),
        daily_notional_used_usd=Decimal(0),
    )


def test_master_halt_blocks_intent() -> None:
    decision = evaluate_wallet_intent(intent(), state(), AgentWalletPolicy())
    assert decision.approved is False
    assert "master halt" in decision.reasons[0]


def test_strict_policy_can_approve_bounded_intent() -> None:
    policy = AgentWalletPolicy(
        allowed_chains=frozenset({Chain.SOLANA}),
        allowed_venues=frozenset({"jupiter"}),
        allowed_contracts=frozenset({"jupiter-program"}),
        max_transaction_usd=Decimal(25),
        max_daily_notional_usd=Decimal(100),
        max_slippage_bps=Decimal(50),
        minimum_reserve_usd=Decimal(100),
        require_evidence=True,
        master_halt=False,
    )
    assert evaluate_wallet_intent(intent(), state(), policy).approved is True


def test_unknown_contract_is_blocked() -> None:
    policy = AgentWalletPolicy(
        allowed_chains=frozenset({Chain.SOLANA}),
        allowed_venues=frozenset({"jupiter"}),
        allowed_contracts=frozenset({"allowed"}),
        master_halt=False,
    )
    result = evaluate_wallet_intent(intent(), state(), policy)
    assert result.approved is False
    assert "allowlisted" in " ".join(result.reasons)


def test_dedicated_wallet_uses_deposited_capital_as_ceiling_but_keeps_owner_halt(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_WALLET_MASTER_HALT", "0")
    dedicated = intent(
        action="sol_transfer",
        venue="solana-mainnet",
        mission_id="mission-1",
        amount_atomic=500,
        fee_limit_lamports=5_000,
    )
    policy = dedicated_wallet_policy()
    policy = AgentWalletPolicy(**{
        **policy.__dict__,
        "allowed_venues": frozenset({"solana-mainnet"}),
    })
    poor_usd_estimate = AgentWalletState(
        estimated_wallet_value_usd=Decimal("0.01"),
        available_cash_like_usd=Decimal("0.01"),
        daily_notional_used_usd=Decimal(1000000),
    )
    assert evaluate_wallet_intent(dedicated, poor_usd_estimate, policy).approved is True
    halted = AgentWalletPolicy(**{**policy.__dict__, "master_halt": True})
    decision = evaluate_wallet_intent(dedicated, poor_usd_estimate, halted)
    assert decision.approved is False
    assert any("master halt" in reason for reason in decision.reasons)


def test_zero_value_operational_validation_only_allows_base_self_call(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_WALLET_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_EVM_ADDRESS", "0x" + "1" * 40)
    base_call = intent(
        chain=Chain.BASE,
        venue="base-mainnet",
        action="evm_native_transfer",
        asset_in="ETH",
        asset_out="ETH",
        notional_usd=Decimal(0),
        expected_slippage_bps=Decimal(0),
        destination="0x" + "1" * 40,
        mission_id="mission-validation",
        amount_atomic=0,
        fee_limit_wei=150_000_000_000,
        operational_validation=True,
    )
    assert evaluate_wallet_intent(base_call, state(), dedicated_wallet_policy()).approved
    redirected = WalletIntent(**{**base_call.__dict__, "destination": "0x" + "2" * 40})
    decision = evaluate_wallet_intent(redirected, state(), dedicated_wallet_policy())
    assert not decision.approved
    assert any("zero-value Base self-call" in reason for reason in decision.reasons)
