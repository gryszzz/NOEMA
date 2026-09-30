import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from noema.execution_gateway import (
    ExecutionGateway,
    ExecutionProposal,
    PredictionExecutionAuthority,
    ReconciliationObservation,
)
from noema.models import Forecast, MarketSnapshot, Mode, Opportunity
from noema.risk import RiskEngine, RiskPolicy


class FakeOrderVenue:
    supports_live_execution = True
    supports_authoritative_reconciliation = True
    execution_economics_verified = True
    execution_account_state_current = True

    def __init__(self):
        self.calls = 0

    async def execute(self, action, *, client_order_id):
        self.calls += 1
        return "test-order-1"

    async def reconcile_execution(self, request):
        return None


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
    proposal = replace(proposal, strategy_id="strategy-test", experiment_id="experiment-test")
    result = ExecutionGateway(str(tmp_path / "gateway.db")).record_proposal(proposal)
    assert result.status == "proposed"
    assert result.tier == "proposal"
    assert result.allowed is False
    with sqlite3.connect(tmp_path / "gateway.db") as conn:
        row = conn.execute("select request_json from execution_gateway_requests").fetchone()
    payload = json.loads(row[0])
    assert payload["decision_id"] == proposal.proposal_id
    assert payload["strategy_id"] == "strategy-test"
    assert payload["experiment_id"] == "experiment-test"


def test_live_preflight_requires_stable_provider_reconciliation_capability(tmp_path):
    proposal, opportunity, action, risk = _proposal()

    class NoReconciliationVenue(FakeOrderVenue):
        supports_authoritative_reconciliation = False

    reasons = ExecutionGateway(str(tmp_path / "gateway.db"))._prediction_checks(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=NoReconciliationVenue(), bankroll_usd=500,
    )
    assert "venue adapter cannot reconcile a dispatch by stable provider identity" in reasons


def test_transition_history_is_append_only_and_provider_bound(tmp_path, monkeypatch):
    import sqlite3

    gateway = ExecutionGateway(str(tmp_path / "gateway.db"))
    gateway._record("p1", "prediction", "execution", "kalshi:production",
                    "unknown", Decimal(2), {"instrument": "KX", "mission_id": "mission-1"}, [])
    observation = ReconciliationObservation(
        provider="kalshi:production", proposal_id="p1", provider_reference="o1",
        status="filled", observed_at=datetime.now(UTC),
        source="kalshi_authenticated_orders_api", filled_quantity=Decimal(3),
        filled_exposure_usd=Decimal(2), remaining_reserved_usd=Decimal(0),
    )
    assert not gateway.apply_reconciliation(replace(observation, provider="kalshi:demo"))
    assert gateway.apply_reconciliation(observation)
    assert gateway.apply_reconciliation(observation)  # identical cumulative snapshot is idempotent
    with sqlite3.connect(gateway.db_path) as conn:
        assert conn.execute("SELECT status,provider_reference FROM execution_gateway_requests").fetchone() == (
            "filled", "o1",
        )
        transitions = conn.execute(
            "SELECT from_status,to_status,source FROM execution_gateway_transitions ORDER BY transition_id"
        ).fetchall()
        assert transitions == [
            (None, "unknown", "gateway"),
            ("unknown", "filled", "kalshi_authenticated_orders_api"),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE execution_gateway_transitions SET to_status='failed'")
    assert gateway._mission_exposure("mission-1", "exclude") == Decimal(2)
    assert gateway._daily_exposure("exclude") == Decimal(2)
    monkeypatch.setenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "3")
    next_proposal, _, _, _ = _proposal(market_id="KXTEST-EVENT-B", proposal_id="p-next")
    assert not gateway._reserve_prediction(next_proposal, {}, _authority())


@pytest.mark.asyncio
async def test_partial_fill_snapshots_advance_cumulatively_and_terminal_fill_stays_exposed(tmp_path):
    import sqlite3

    gateway = ExecutionGateway(str(tmp_path / "gateway.db"))
    gateway._record("p-partial", "prediction", "execution", "kalshi:production",
                    "unknown", Decimal(2), {"mission_id": "mission-1"}, [])
    def observation(status, filled, remaining, fee, fill_ids):
        return ReconciliationObservation(
            provider="kalshi:production", proposal_id="p-partial", provider_reference="order-1",
            status=status, observed_at=datetime.now(UTC), source="kalshi_authenticated_orders_api",
            filled_quantity=filled, fee_amount=fee, fee_currency="USD",
            external_fill_ids=tuple(fill_ids), filled_exposure_usd=filled,
            remaining_reserved_usd=remaining,
        )

    first = observation("partial", Decimal("0.50"), Decimal("1.50"), Decimal("0.01"), ["f1"])
    second = observation("partial", Decimal("1.00"), Decimal("1.00"), Decimal("0.02"), ["f1", "f2"])
    final = observation("filled", Decimal("1.75"), Decimal(0), Decimal("0.03"), ["f1", "f2", "f3"])
    assert gateway.apply_reconciliation(first)
    assert gateway.apply_reconciliation(second)
    assert gateway.apply_reconciliation(second)
    regressive = replace(second, fee_amount=Decimal("0.015"), external_fill_ids=("f1",))
    assert not gateway.apply_reconciliation(regressive)
    class FinalSnapshotAdapter:
        async def reconcile_execution(self, request):
            assert request["status"] == "partial"
            return final

    assert await gateway.reconcile_pending(lambda _venue: FinalSnapshotAdapter()) == {
        "checked": 1, "resolved": 1, "unchanged": 0,
    }
    assert gateway.apply_reconciliation(final)
    with sqlite3.connect(gateway.db_path) as conn:
        history = conn.execute(
            "SELECT to_status FROM execution_gateway_transitions WHERE proposal_id='p-partial' ORDER BY transition_id"
        ).fetchall()
        row = conn.execute(
            "SELECT status,filled_exposure_usd,remaining_reserved_usd,result_json "
            "FROM execution_gateway_requests WHERE proposal_id='p-partial'"
        ).fetchone()
    assert [item[0] for item in history] == ["unknown", "partial", "partial", "filled"]
    assert row[:3] == ("filled", "1.75", "0")
    final_evidence = __import__("json").loads(row[3])
    assert final_evidence["external_fill_ids"] == ["f1", "f2", "f3"]
    assert final_evidence["fee_amount"] == "0.03"
    assert gateway._mission_exposure("mission-1", "exclude") == Decimal("1.75")


@pytest.mark.parametrize("terminal", ["rejected", "cancelled", "expired", "failed"])
def test_authoritative_zero_fill_terminal_releases_only_unfilled_reservation(tmp_path, terminal):
    gateway = ExecutionGateway(str(tmp_path / f"{terminal}.db"))
    gateway._record("p-terminal", "prediction", "execution", "kalshi:production",
                    "unknown", Decimal(2), {"mission_id": "mission-1"}, [])
    result = ReconciliationObservation(
        provider="kalshi:production", proposal_id="p-terminal", provider_reference="order-terminal",
        status=terminal, observed_at=datetime.now(UTC), source="provider_orders_api",
        filled_exposure_usd=Decimal(0), remaining_reserved_usd=Decimal(0),
    )
    assert gateway.apply_reconciliation(result)
    assert gateway._mission_exposure("mission-1", "exclude") == Decimal(0)
    assert gateway.apply_reconciliation(result)
    regressive = replace(result, status="partial", filled_exposure_usd=Decimal("0.25"),
                         remaining_reserved_usd=Decimal("0.25"))
    assert not gateway.apply_reconciliation(regressive)
    assert gateway._mission_exposure("mission-1", "exclude") == Decimal(0)


def test_partial_cancel_preserves_filled_exposure_and_releases_remainder(tmp_path):
    gateway = ExecutionGateway(str(tmp_path / "cancel.db"))
    gateway._record("p-cancel", "prediction", "execution", "kalshi:production",
                    "unknown", Decimal(2), {"mission_id": "mission-1"}, [])
    partial = ReconciliationObservation(
        provider="kalshi:production", proposal_id="p-cancel", provider_reference="order-cancel",
        status="partial", observed_at=datetime.now(UTC), source="provider_orders_api",
        filled_exposure_usd=Decimal("0.60"), remaining_reserved_usd=Decimal("1.40"),
        external_fill_ids=("fill-one",),
    )
    cancelled = replace(partial, status="cancelled", remaining_reserved_usd=Decimal(0))
    assert gateway.apply_reconciliation(partial)
    assert gateway.apply_reconciliation(cancelled)
    assert gateway._mission_exposure("mission-1", "exclude") == Decimal("0.60")
    assert not gateway.apply_reconciliation(replace(cancelled, status="filled"))


@pytest.mark.parametrize("terminal", ["rejected", "cancelled", "expired", "failed"])
def test_partial_terminal_state_releases_only_remaining_reservation(tmp_path, terminal):
    gateway = ExecutionGateway(str(tmp_path / f"partial-{terminal}.db"))
    gateway._record("p-partial-terminal", "prediction", "execution", "kalshi:production",
                    "unknown", Decimal(2), {"mission_id": "mission-1"}, [])
    partial = ReconciliationObservation(
        provider="kalshi:production", proposal_id="p-partial-terminal", provider_reference="partial-order",
        status="partial", observed_at=datetime.now(UTC), source="provider_orders_api",
        filled_quantity=Decimal(1), fee_amount=Decimal("0.01"), fee_currency="USD",
        external_fill_ids=("fill-kept",), filled_exposure_usd=Decimal("0.60"),
        remaining_reserved_usd=Decimal("1.40"),
    )
    final = replace(partial, status=terminal, remaining_reserved_usd=Decimal(0))
    assert gateway.apply_reconciliation(partial)
    assert gateway.apply_reconciliation(final)
    assert gateway._mission_exposure("mission-1", "exclude") == Decimal("0.60")
    assert gateway._daily_exposure("exclude") == Decimal("0.60")
    assert not gateway.apply_reconciliation(replace(final, status="partial"))
    assert gateway._mission_exposure("mission-1", "exclude") == Decimal("0.60")


@pytest.mark.asyncio
async def test_reconciler_keeps_unknown_when_provider_has_no_exact_evidence(tmp_path):
    gateway = ExecutionGateway(str(tmp_path / "gateway.db"))
    gateway._record("p2", "prediction", "execution", "kalshi:production",
                    "unknown", Decimal(2), {"instrument": "KX"}, [])

    class UncertainAdapter:
        async def reconcile_execution(self, _request):
            return None

    result = await gateway.reconcile_pending(lambda _venue: UncertainAdapter())
    assert result == {"checked": 1, "resolved": 0, "unchanged": 1}
    assert gateway.overview()["recent_requests"][0]["status"] == "unknown"
    assert gateway._daily_exposure("not-this-request") == Decimal(2)


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
@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (
            {"evidence_refs": ("unrelated-evidence",)},
            "proposal evidence does not match the immutable forecast evidence",
        ),
        (
            {"expected_edge": Decimal("0.37")},
            "proposal expected edge does not match the deterministic opportunity",
        ),
    ],
)
async def test_prediction_proposal_rejects_unbound_evidence_or_edge(
    tmp_path, monkeypatch, tamper, reason,
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
    proposal = replace(proposal, **tamper)
    venue = FakeOrderVenue()
    gateway = ExecutionGateway(
        str(tmp_path / "gateway.db"), authority_resolver=lambda _mission: _authority(),
    )

    result = await gateway.submit_prediction_order(
        proposal=proposal, opportunity=opportunity, action=action,
        risk_engine=risk, venue_adapter=venue, bankroll_usd=500,
    )

    assert not result.allowed
    assert result.status == "rejected"
    assert reason in result.reasons
    assert venue.calls == 0


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
        async def execute(self, action, *, client_order_id):
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
