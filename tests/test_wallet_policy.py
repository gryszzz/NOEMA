from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from noema.wallet_intents import WalletIntent
from noema.wallet_policy import (
    AgentWalletPolicy,
    AgentWalletState,
    FinancialMissionAuthority,
    dedicated_wallet_policy,
    evaluate_wallet_intent,
)
from noema.wallet_types import Chain

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def intent(**overrides) -> WalletIntent:
    values = {
        "intent_id": "i1", "chain": Chain.SOLANA, "venue": "jupiter",
        "action": "sol_transfer", "asset_in": "USDC", "asset_out": "SOL",
        "notional_usd": Decimal(10), "expected_slippage_bps": Decimal(20),
        "contract_or_program": "jupiter-program", "evidence_ids": ("e1",),
        "mission_id": "mission-1", "wallet_id": "wallet-1", "network": "solana-mainnet",
        "amount_atomic": 10, "fee_limit_lamports": 5000,
        "expires_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return WalletIntent(**values)


def state(**overrides) -> AgentWalletState:
    values = {
        "estimated_wallet_value_usd": Decimal(500),
        "available_cash_like_usd": Decimal(300),
        "daily_notional_used_usd": Decimal(0),
    }
    values.update(overrides)
    return AgentWalletState(**values)


def policy(**overrides) -> AgentWalletPolicy:
    values = {
        "allowed_chains": frozenset({Chain.SOLANA}),
        "allowed_venues": frozenset({"jupiter"}),
        "allowed_contracts": frozenset({"jupiter-program"}),
        "max_transaction_usd": Decimal(25), "max_daily_notional_usd": Decimal(100),
        "max_slippage_bps": Decimal(50), "minimum_reserve_usd": Decimal(0),
        "require_evidence": True, "master_halt": False,
    }
    values.update(overrides)
    return AgentWalletPolicy(**values)


def authority(**overrides) -> FinancialMissionAuthority:
    values = {
        "authority_id": "authority-1", "mission_id": "mission-1", "wallet_id": "wallet-1",
        "allowed_chains": frozenset({Chain.SOLANA}),
        "allowed_networks": frozenset({"solana-mainnet"}),
        "allowed_actions": frozenset({"sol_transfer"}),
        "allowed_venues": frozenset({"jupiter"}),
        "allowed_protocols": frozenset({"jupiter-program"}),
        "allowed_assets": frozenset({"USDC", "SOL"}),
        "per_action_limit_usd": Decimal(20), "per_mission_limit_usd": Decimal(50),
        "daily_limit_usd": Decimal(100), "maximum_exposure_usd": Decimal(40),
        "expires_at": NOW + timedelta(hours=1), "capital_pool_id": "pool-1",
    }
    values.update(overrides)
    return FinancialMissionAuthority(**values)


def decide(*, action=None, mission_status="running", **kwargs):
    return evaluate_wallet_intent(
        intent(**({} if action is None else action)), state(), policy(),
        authority=authority(), mission_status=mission_status,
        mission_exposure_usd=Decimal(0),
        daily_exposure_usd=Decimal(0),
        live_execution_enabled=True, signer_isolated=True, state_is_current=True, now=NOW, **kwargs,
    )


def test_complete_explicit_authority_can_pass_pure_policy_validation() -> None:
    assert decide().approved


@pytest.mark.parametrize("mission_status", [None, "discovered", "queued", "claimed", "completed", "failed"])
def test_unknown_or_inactive_mission_is_denied(mission_status) -> None:
    result = decide(mission_status=mission_status)
    assert not result.approved
    assert any(reason in " ".join(result.reasons) for reason in ("mission does not exist", "not currently active"))


@pytest.mark.parametrize(("patch", "reason"), [
    ({"allowed_chains": frozenset({Chain.BASE})}, "this chain"),
    ({"allowed_networks": frozenset({"base-mainnet"})}, "this network"),
    ({"wallet_id": "another-wallet"}, "does not match wallet"),
    ({"allowed_actions": frozenset({"evm_native_transfer"})}, "this action"),
    ({"allowed_venues": frozenset({"other-venue"})}, "this venue"),
    ({"allowed_protocols": frozenset({"other-program"})}, "this protocol"),
    ({"allowed_assets": frozenset({"USDC"})}, "these assets"),
    ({"per_action_limit_usd": None}, "per-action cap"),
    ({"per_action_limit_usd": Decimal(0)}, "per-action cap"),
    ({"per_mission_limit_usd": Decimal(0)}, "per-mission cap"),
    ({"daily_limit_usd": Decimal(0)}, "daily cap"),
    ({"maximum_exposure_usd": Decimal(0)}, "maximum exposure cap"),
])
def test_authority_scope_and_missing_or_zero_caps_fail_closed(patch, reason) -> None:
    result = evaluate_wallet_intent(
        intent(), state(), policy(), authority=authority(**patch), mission_status="running",
        mission_exposure_usd=Decimal(0),
        daily_exposure_usd=Decimal(0), live_execution_enabled=True, signer_isolated=True,
        state_is_current=True, now=NOW,
    )
    assert not result.approved
    assert any(reason in item for item in result.reasons)


def test_per_action_mission_daily_and_total_exposure_caps_are_enforced() -> None:
    cases = [
        ({}, Decimal(21), Decimal(0), Decimal(0), "per-action cap"),
        ({}, Decimal(10), Decimal(45), Decimal(0), "mission cumulative cap"),
        ({}, Decimal(10), Decimal(0), Decimal(95), "mission daily cap"),
        ({}, Decimal(10), Decimal(35), Decimal(0), "maximum exposure"),
    ]
    for overrides, amount, mission_used, daily_used, reason in cases:
        result = evaluate_wallet_intent(
        intent(notional_usd=amount), state(), policy(), authority=authority(**overrides),
        mission_status="running", mission_exposure_usd=mission_used,
            daily_exposure_usd=daily_used, live_execution_enabled=True,
            signer_isolated=True, state_is_current=True, now=NOW,
        )
        assert not result.approved
        assert any(reason in item for item in result.reasons)


def test_expired_intent_and_revoked_or_expired_authority_are_denied() -> None:
    expired_intent = evaluate_wallet_intent(
        intent(expires_at=NOW), state(), policy(), authority=authority(), mission_status="running",
        mission_exposure_usd=Decimal(0),
        daily_exposure_usd=Decimal(0),
        live_execution_enabled=True, signer_isolated=True, state_is_current=True, now=NOW,
    )
    expired_authority = evaluate_wallet_intent(
        intent(), state(), policy(),
        authority=authority(expires_at=NOW - timedelta(seconds=1)), mission_status="running",
        mission_exposure_usd=Decimal(0),
        daily_exposure_usd=Decimal(0),
        live_execution_enabled=True, signer_isolated=True, state_is_current=True, now=NOW,
    )
    assert not expired_intent.approved and "intent is expired or has no valid expiry" in expired_intent.reasons
    assert not expired_authority.approved and "mission financial authority is expired" in expired_authority.reasons


def test_replay_halt_signer_context_and_live_switch_fail_closed() -> None:
    checks = [
        ({"intent_consumed": True}, "already been consumed"),
        ({"live_execution_enabled": False}, "live execution is disabled"),
        ({"signer_isolated": False}, "not isolated"),
    ]
    for kwargs, reason in checks:
        checks_kwargs = {"intent_consumed": False, "live_execution_enabled": True, "signer_isolated": True}
        checks_kwargs.update(kwargs)
        result = evaluate_wallet_intent(
            intent(), state(), policy(), authority=authority(), mission_status="running",
            mission_exposure_usd=Decimal(0),
            daily_exposure_usd=Decimal(0), now=NOW, state_is_current=True, **checks_kwargs,
        )
        assert not result.approved and any(reason in item for item in result.reasons)
    halted = evaluate_wallet_intent(
        intent(), state(), policy(master_halt=True), authority=authority(), mission_status="running",
        mission_exposure_usd=Decimal(0),
        daily_exposure_usd=Decimal(0),
        live_execution_enabled=True, signer_isolated=True, state_is_current=True, now=NOW,
    )
    assert not halted.approved and "agent wallet master halt enabled" in halted.reasons


@pytest.mark.parametrize("field", ["max_transaction_usd", "max_daily_notional_usd"])
def test_missing_or_zero_global_caps_deny_even_for_dedicated_pool(field) -> None:
    for cap in (None, Decimal(0)):
        result = evaluate_wallet_intent(
            intent(), state(), policy(**{field: cap, "dedicated_capital_authority": True}),
            authority=authority(), mission_status="running", live_execution_enabled=True,
            mission_exposure_usd=Decimal(0),
            daily_exposure_usd=Decimal(0), signer_isolated=True, state_is_current=True, now=NOW,
        )
        assert not result.approved
        assert any("cap is missing or nonpositive" in reason for reason in result.reasons)


def test_dedicated_capital_default_is_zero_bounded_and_denied() -> None:
    result = evaluate_wallet_intent(
        intent(), state(), dedicated_wallet_policy(), authority=authority(),
        mission_status="running", mission_exposure_usd=Decimal(0), daily_exposure_usd=Decimal(0), live_execution_enabled=True, signer_isolated=True,
        state_is_current=True, now=NOW,
    )
    assert not result.approved
    assert "global per-transaction cap is missing or nonpositive" in result.reasons


@pytest.mark.parametrize("source", [
    "research", "retrieval", "knowledge", "social", "prompt", "specialist_output",
])
def test_untrusted_information_sources_cannot_grant_financial_authority(source) -> None:
    # Source labels and payloads remain evidence; none are authority objects.
    result = evaluate_wallet_intent(
        intent(evidence_ids=(source, f"{source}-result")), state(), policy(),
        mission_status="running", mission_exposure_usd=Decimal(0), daily_exposure_usd=Decimal(0), live_execution_enabled=True, signer_isolated=True,
        state_is_current=True, now=NOW,
    )
    assert not result.approved
    assert "explicit financial mission authority is absent" in result.reasons
