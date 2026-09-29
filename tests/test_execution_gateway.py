from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from noema.execution_gateway import (
    ExecutionGateway,
    ExecutionProposal,
    PredictionExecutionAuthority,
)
from noema.models import Forecast, MarketSnapshot, Mode, Opportunity
from noema.risk import RiskEngine, RiskPolicy


class FakeOrderVenue:
    supports_live_execution = True
    execution_economics_verified = True
    execution_account_state_current = True

    def __init__(self):
        self.calls = 0

    async def execute(self, action):
        self.calls += 1
        return "test-order-1"


def _proposal(market_id="KXTEST-EVENT-A", proposal_id="proposal-1"):
    now = datetime.now(UTC)
    snapshot = MarketSnapshot(
        venue="kalshi:production", market_id=market_id, title="Test event",
        yes_bid=.50, yes_ask=.51, no_bid=.49, no_ask=.50, liquidity_usd=5000,
        closes_at=None, resolution_rules="Test resolution", captured_at=now,
    )
    forecast = Forecast(
        market_id=snapshot.market_id, venue=snapshot.venue,
        probability_yes=.90, lower_bound=.88, upper_bound=.92,
        model_version="test", evidence_ids=("evidence-1",), created_at=now,
    )
    opportunity = Opportunity(
        forecast=forecast, snapshot=snapshot, market_probability=.51,
        raw_edge=.39, estimated_cost=.01, uncertainty_penalty=.02,
        robust_edge=.36,
    )
    risk = RiskEngine(RiskPolicy(
        mode=Mode.LIVE, min_robust_edge=.01, min_liquidity_usd=1,
        max_spread=.1, max_stake_usd=10, max_fraction_of_bankroll=.01,
        max_forecast_width=.1, max_market_data_age_seconds=30,
    ))
    action = risk.decide(opportunity, bankroll_usd=500)
    proposal = ExecutionProposal.from_opportunity(
        proposal_id=proposal_id, mission_id="mission-1", opportunity=opportunity,
        action=action, evidence_refs=forecast.evidence_ids,
        expires_at=now + timedelta(seconds=20),
    )
    return proposal, opportunity, action, risk


def _authority():
    return PredictionExecutionAuthority(
        authority_id="owner-limit-1", mission_id="mission-1",
        allowed_venues=frozenset({"kalshi:production"}),
        allowed_actions=frozenset({"buy_yes"}),
        allowed_instruments=frozenset({"KXTEST-EVENT-A", "KXTEST-EVENT-B"}),
        per_action_limit_usd=Decimal(5), per_mission_limit_usd=Decimal(5),
        daily_limit_usd=Decimal(5), maximum_exposure_usd=Decimal(5),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_prediction_action_fails_closed_and_is_audited_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("NOEMA_EXECUTION_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("NOEMA_ALLOW_LIVE_ORDERS", raising=False)
    proposal, opportunity, action, risk = _proposal()
    venue = FakeOrderVenue()
    gateway = ExecutionGateway(
        str(tmp_path / "gateway.db"), authority_resolver=lambda _mission: _authority(),
    )

    result = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )

    assert not result.allowed
    assert "execution gateway is disabled" in result.reasons
    assert venue.calls == 0
    with __import__("sqlite3").connect(gateway.db_path) as conn:
        row = conn.execute(
            "SELECT status,tier,provider_reference FROM execution_gateway_requests"
        ).fetchone()
    assert row == ("rejected", "execution", None)


def test_workstation_read_is_read_only_when_gateway_store_does_not_exist(tmp_path):
    db_path = tmp_path / "not-created.db"
    result = ExecutionGateway(str(db_path), initialize=False).overview()
    assert result["status"] == "FAIL CLOSED · NOT ARMED"
    assert result["recent_requests"] == []
    assert not db_path.exists()


def test_structured_proposal_record_does_not_grant_execution(tmp_path):
    proposal, _, _, _ = _proposal()
    result = ExecutionGateway(str(tmp_path / "gateway.db")).record_proposal(proposal)
    assert result.status == "proposed"
    assert result.tier == "proposal"
    assert result.allowed is False


@pytest.mark.asyncio
async def test_prediction_submission_requires_economics_and_consumes_idempotency_key(
    tmp_path, monkeypatch,
):
    for name, value in {
        "NOEMA_EXECUTION_GATEWAY_ENABLED": "1",
        "NOEMA_ALLOW_LIVE_ORDERS": "1",
        "NOEMA_MASTER_HALT": "0",
        "NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD": "5",
        "NOEMA_LIVE_VENUES": "kalshi:production",
    }.items():
        monkeypatch.setenv(name, value)
    proposal, opportunity, action, risk = _proposal()
    venue = FakeOrderVenue()
    gateway = ExecutionGateway(
        str(tmp_path / "gateway.db"), authority_resolver=lambda _mission: _authority(),
    )

    first = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )
    replay = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )

    assert first.allowed and first.provider_reference == "test-order-1"
    assert not replay.allowed
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_ambiguous_provider_error_keeps_exposure_and_instrument_reserved(
    tmp_path, monkeypatch,
):
    for name, value in {
        "NOEMA_EXECUTION_GATEWAY_ENABLED": "1",
        "NOEMA_ALLOW_LIVE_ORDERS": "1",
        "NOEMA_MASTER_HALT": "0",
        "NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD": "5",
        "NOEMA_LIVE_VENUES": "kalshi:production",
    }.items():
        monkeypatch.setenv(name, value)
    proposal, opportunity, action, risk = _proposal()

    class AmbiguousVenue(FakeOrderVenue):
        async def execute(self, action):
            self.calls += 1
            raise TimeoutError("provider response unavailable")

    venue = AmbiguousVenue()
    gateway = ExecutionGateway(
        str(tmp_path / "gateway.db"), authority_resolver=lambda _mission: _authority(),
    )
    with pytest.raises(TimeoutError):
        await gateway.submit_prediction_order(
            proposal=proposal, opportunity=opportunity, action=action,
            risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
        )

    replay, replay_opportunity, replay_action, replay_risk = _proposal(
        market_id="KXTEST-EVENT-B", proposal_id="proposal-2",
    )
    blocked = await gateway.submit_prediction_order(
        proposal=replay, opportunity=replay_opportunity, action=replay_action,
        risk_engine=replay_risk, venue_adapter=venue, bankroll_usd=500,
    )
    assert not blocked.allowed
    assert venue.calls == 1
    assert gateway._daily_exposure("proposal-2") == proposal.notional_usd
    with __import__("sqlite3").connect(gateway.db_path) as conn:
        row = conn.execute(
            "SELECT status,result_json FROM execution_gateway_requests WHERE proposal_id=?",
            (proposal.proposal_id,),
        ).fetchone()
    assert row[0] == "unknown"
    assert "reconciliation required" in row[1]


@pytest.mark.asyncio
async def test_revoked_mission_authority_is_rechecked_before_provider_call(tmp_path, monkeypatch):
    for name, value in {
        "NOEMA_EXECUTION_GATEWAY_ENABLED": "1",
        "NOEMA_ALLOW_LIVE_ORDERS": "1",
        "NOEMA_MASTER_HALT": "0",
        "NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD": "5",
        "NOEMA_LIVE_VENUES": "kalshi:production",
    }.items():
        monkeypatch.setenv(name, value)
    proposal, opportunity, action, risk = _proposal()
    venue = FakeOrderVenue()
    resolutions = 0

    def resolve(_mission):
        nonlocal resolutions
        resolutions += 1
        authority = _authority()
        return replace(authority, revoked=True) if resolutions >= 3 else authority

    gateway = ExecutionGateway(str(tmp_path / "gateway.db"), authority_resolver=resolve)
    result = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )

    assert not result.allowed
    assert "mission execution authority is revoked or mismatched" in result.reasons
    assert venue.calls == 0
    assert gateway._daily_exposure("not-excluded") == Decimal(0)


@pytest.mark.asyncio
async def test_prediction_execution_without_mission_authority_never_calls_venue(
    tmp_path, monkeypatch,
):
    for name, value in {
        "NOEMA_EXECUTION_GATEWAY_ENABLED": "1",
        "NOEMA_ALLOW_LIVE_ORDERS": "1",
        "NOEMA_MASTER_HALT": "0",
        "NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD": "5",
        "NOEMA_LIVE_VENUES": "kalshi:production",
    }.items():
        monkeypatch.setenv(name, value)
    proposal, opportunity, action, risk = _proposal()
    venue = FakeOrderVenue()
    gateway = ExecutionGateway(str(tmp_path / "gateway.db"))

    result = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )

    assert not result.allowed
    assert "separate mission execution authority is absent" in result.reasons
    assert venue.calls == 0


@pytest.mark.asyncio
async def test_wallet_transfer_requires_separate_treasury_permission(tmp_path, monkeypatch):
    from noema.wallet_intents import WalletIntent
    from noema.wallet_types import Chain

    monkeypatch.delenv("NOEMA_TREASURY_ACTIONS_ENABLED", raising=False)
    gateway = ExecutionGateway(str(tmp_path / "gateway.db"))
    intent = WalletIntent(
        intent_id="wallet-intent-1", chain=Chain.BASE, venue="base-mainnet",
        action="evm_native_transfer", asset_in="ETH", asset_out="ETH",
        notional_usd=Decimal(1), expected_slippage_bps=Decimal(0),
    )
    calls = 0

    async def handler():
        nonlocal calls
        calls += 1
        raise AssertionError("treasury-disabled intent must not reach wallet policy or signer")

    result = await gateway.submit_wallet_intent(intent=intent, handler=handler)

    assert not result.decision.approved
    assert calls == 0
    assert "owner treasury permission is disabled" in result.decision.reasons
