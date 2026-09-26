from decimal import Decimal

from noema.wallet_intents import WalletIntent
from noema.wallet_policy import AgentWalletPolicy, AgentWalletState, evaluate_wallet_intent
from noema.wallet_types import Chain


def intent(**overrides) -> WalletIntent:
    values = {
        "intent_id": "i1",
        "chain": Chain.SOLANA,
        "venue": "jupiter",
        "action": "swap",
        "asset_in": "USDC",
        "asset_out": "SOL",
        "notional_usd": Decimal("10"),
        "expected_slippage_bps": Decimal("20"),
        "contract_or_program": "jupiter-program",
        "evidence_ids": ("e1",),
    }
    values.update(overrides)
    return WalletIntent(**values)


def state() -> AgentWalletState:
    return AgentWalletState(
        estimated_wallet_value_usd=Decimal("500"),
        available_cash_like_usd=Decimal("300"),
        daily_notional_used_usd=Decimal("0"),
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
        max_transaction_usd=Decimal("25"),
        max_daily_notional_usd=Decimal("100"),
        max_slippage_bps=Decimal("50"),
        minimum_reserve_usd=Decimal("100"),
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
