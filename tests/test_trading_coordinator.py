import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from noema.dashboard_app import app
from noema.economic_ledger import EconomicLedger
from noema.economic_models import AutonomyEvidence, AutonomyLevel
from noema.execution_gateway import ExecutionGateway, ReconciliationObservation
from noema.fill_accounting import summarize_fills
from noema.models import Forecast, MarketSnapshot, Opportunity
from noema.risk import RiskEngine, RiskPolicy
from noema.trade_telemetry import ObservedFill, ObservedOrder
from noema.trading_coordinator import TradingCoordinator


class FakeVenue:
    name = "kalshi:production"
    supports_live_execution = True
    supports_authoritative_reconciliation = True
    execution_economics_verified = True
    execution_account_state_current = True

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.snapshots: list[ReconciliationObservation] = []
        self.halt_after: int | None = None
        self.balance: str | None = None

    async def execute(self, action, *, client_order_id):
        self.calls += 1
        if self.halt_after is not None and self.calls >= self.halt_after:
            __import__("os").environ["NOEMA_MASTER_HALT"] = "1"
        return f"order-{client_order_id[:8]}"

    async def cancel(self, order_id: str) -> str:
        self.cancelled.append(order_id)
        return order_id

    async def close_position(self, market_id: str) -> str:
        self.closed.append(market_id)
        return market_id

    async def balances(self):
        if self.balance is None:
            raise RuntimeError("balance unavailable")
        return {"buying_power_usd": self.balance}

    async def reconcile_execution(self, request):
        if not self.snapshots:
            return None
        snapshot = self.snapshots.pop(0)
        return snapshot

    async def stream_private(self):
        return []

    async def order_status(self, _order_id):
        return None


def _paper_evidence() -> AutonomyEvidence:
    return AutonomyEvidence(
        resolved_forecasts=40,
        live_observation_days=1,
        after_cost_return=Decimal(0),
        realized_net_pnl_usd=Decimal(0),
        max_drawdown_fraction=Decimal(1),
        calibration_error=None,
        reconciliation_ok_fraction=Decimal(0),
        profitable_days=0,
        losing_days=1,
    )


def _micro_evidence() -> AutonomyEvidence:
    return AutonomyEvidence(
        resolved_forecasts=300,
        live_observation_days=30,
        after_cost_return=Decimal("0.01"),
        realized_net_pnl_usd=Decimal(10),
        max_drawdown_fraction=Decimal("0.04"),
        calibration_error=Decimal("0.06"),
        reconciliation_ok_fraction=Decimal("0.996"),
        profitable_days=5,
        losing_days=1,
    )


def _shadow_evidence() -> AutonomyEvidence:
    return AutonomyEvidence(
        resolved_forecasts=3,
        live_observation_days=0,
        after_cost_return=Decimal(0),
        realized_net_pnl_usd=Decimal(0),
        max_drawdown_fraction=Decimal(0),
        calibration_error=None,
        reconciliation_ok_fraction=Decimal(0),
        profitable_days=0,
        losing_days=0,
    )


def _opportunity(
    market_id: str = "m1", when: datetime | None = None, edge: float = 0.36,
) -> Opportunity:
    moment = when or datetime.now(UTC)
    snapshot = MarketSnapshot(
        venue="kalshi:production", market_id=market_id, title="Test",
        yes_bid=0.50, yes_ask=0.51, no_bid=0.48, no_ask=0.49,
        liquidity_usd=5000, closes_at=None, resolution_rules="Objective binary resolution.",
        captured_at=moment,
    )
    forecast = Forecast(
        market_id=market_id, venue=snapshot.venue, probability_yes=0.90,
        lower_bound=0.88, upper_bound=0.92, model_version="test",
        evidence_ids=("evidence-1",), created_at=moment,
    )
    return Opportunity(
        forecast=forecast, snapshot=snapshot, market_probability=0.51,
        raw_edge=0.39, estimated_cost=0.01, uncertainty_penalty=0.02, robust_edge=edge,
    )


def _open(tmp_path, venue: FakeVenue):
    path = tmp_path / "coordinator.db"
    gateway = ExecutionGateway(str(path))
    ledger = EconomicLedger(str(path))
    coordinator = TradingCoordinator(
        str(path), gateway, RiskEngine(RiskPolicy()), venue, ledger=ledger,
    )
    return path, gateway, ledger, coordinator


def _live_flags(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "kalshi:production")
    monkeypatch.setenv("NOEMA_LIVE_REQUIRES_OWNER_REVIEW", "0")
    monkeypatch.delenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", raising=False)


def _fills(path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT event_type, value_state, amount, evidence_json, external_reference_id "
        "FROM economic_events WHERE event_type='prediction_fill' ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


@pytest.mark.asyncio
async def test_paper_cycle_records_fill_without_calling_the_venue(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NOEMA_EXECUTION_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("NOEMA_ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_LIVE_REQUIRES_OWNER_REVIEW", "1")
    venue = FakeVenue()
    path, _gateway, ledger, coordinator = _open(tmp_path, venue)
    opportunity = _opportunity()
    try:
        report = await coordinator.run_cycle(
            [opportunity], evidence=_paper_evidence(), bankroll_usd=2000,
        )
        replay = await coordinator.run_cycle(
            [opportunity], evidence=_paper_evidence(), bankroll_usd=2000,
        )
    finally:
        coordinator.close()
        ledger.conn.close()

    assert report.mode == "paper"
    assert report.level == "paper"
    assert report.paper_fills == 1
    assert report.submitted == 0
    assert replay.paper_fills == 0
    assert venue.calls == 0
    rows = _fills(path)
    assert len(rows) == 1
    assert rows[0]["value_state"] == "paper"
    evidence = json.loads(rows[0]["evidence_json"])
    assert set(evidence) == {
        "proposal_id", "order_id", "venue", "instrument", "mode", "side",
        "filled_count", "remaining_count", "gross_fill_cost", "fees_paid",
        "average_fill_price", "maker_fraction", "fee_model",
    }
    assert evidence["mode"] == "paper"
    assert evidence["fees_paid"] == "0"
    assert Decimal(evidence["gross_fill_cost"]) > 0
    assert evidence["average_fill_price"] is not None


@pytest.mark.asyncio
async def test_shadow_evidence_does_not_open_orders(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    venue = FakeVenue()
    _path, _gateway, ledger, coordinator = _open(tmp_path, venue)
    try:
        report = await coordinator.run_cycle(
            [_opportunity()], evidence=_shadow_evidence(), bankroll_usd=2000,
        )
    finally:
        coordinator.close()
        ledger.conn.close()
    assert report.level == "shadow"
    assert report.paper_fills == 0
    assert report.submitted == 0
    assert venue.calls == 0


@pytest.mark.asyncio
async def test_live_submit_reconcile_partial_fill_and_restart_are_idempotent(
    tmp_path, monkeypatch,
) -> None:
    _live_flags(monkeypatch)
    venue = FakeVenue()
    venue.balance = "1000"
    path, gateway, ledger, coordinator = _open(tmp_path, venue)
    opportunity = _opportunity()
    coordinator.store_level(AutonomyLevel.MICRO)
    try:
        submitted = await coordinator.run_cycle(
            [opportunity], evidence=_micro_evidence(), bankroll_usd=1000,
        )
        assert submitted.submitted == 1
        assert venue.calls == 1
        row = gateway.request_row(
            f"prediction-markets:kalshi:production:m1:{opportunity.forecast.created_at.astimezone(UTC).isoformat()}"
        )
        assert row is not None
        assert row["status"] == "submitted"
        venue.snapshots.append(_snapshot(
            row, "partial", Decimal("0.50"), Decimal("1.00"), Decimal("9.50"), ("f1",), Decimal("0.01"),
        ))
        first = await coordinator.reconcile()
        venue.snapshots.append(_snapshot(
            row, "filled", Decimal("1.00"), Decimal("2.00"), Decimal(0), ("f1", "f2"), Decimal("0.02"),
        ))
        second = await coordinator.reconcile()
        third = await coordinator.reconcile()
        restart_venue = FakeVenue()
        restart_venue.balance = "1000"
        restarted = TradingCoordinator(
            str(path), gateway, RiskEngine(RiskPolicy()), restart_venue, ledger=ledger,
        )
        restarted.store_level(AutonomyLevel.MICRO)
        again = await restarted.run_cycle(
            [opportunity], evidence=_micro_evidence(), bankroll_usd=1000,
        )
        restarted.close()
    finally:
        coordinator.close()
        ledger.conn.close()

    assert first["booked"] == 1
    assert second["booked"] == 1
    assert third["booked"] == 0
    assert again.submitted == 0
    assert restart_venue.calls == 0
    rows = _fills(path)
    assert [row["value_state"] for row in rows] == ["realized", "realized"]
    assert [json.loads(row["evidence_json"])["mode"] for row in rows] == ["live", "live"]
    assert sum(Decimal(row["amount"]) for row in rows) == Decimal("1.00")
    assert set(json.loads(rows[0]["evidence_json"])) == {
        "proposal_id", "order_id", "venue", "instrument", "mode", "side",
        "filled_count", "remaining_count", "gross_fill_cost", "fees_paid",
        "average_fill_price", "maker_fraction", "fee_model",
    }
    direct = summarize_fills(
        ObservedOrder(
            order_id="check", ticker="m1", status="filled", initial_count=Decimal(1),
            fill_count=Decimal(1), remaining_count=Decimal(0), yes_price=Decimal("0.50"),
            no_price=None, taker_fill_cost=Decimal("0.50"), maker_fill_cost=Decimal(0),
            taker_fees=Decimal("0.01"), maker_fees=Decimal(0), order_group_id=None,
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        [ObservedFill(
            fill_id="f1", order_id="check", ticker="m1", outcome_side="yes",
            count=Decimal(1), yes_price=Decimal("0.50"), no_price=None, is_taker=True,
            fee_cost=Decimal("0.01"), created_at=datetime.now(UTC),
        )],
    )
    first_evidence = json.loads(rows[0]["evidence_json"])
    assert Decimal(first_evidence["average_fill_price"]) == direct.average_fill_price
    assert Decimal(first_evidence["fees_paid"]) == direct.fees_paid


@pytest.mark.asyncio
async def test_stale_unfilled_order_is_cancelled_after_ttl(tmp_path, monkeypatch) -> None:
    _live_flags(monkeypatch)
    monkeypatch.setenv("NOEMA_ORDER_TTL_SECONDS", "30")
    venue = FakeVenue()
    venue.balance = "1000"
    path, _gateway, ledger, coordinator = _open(tmp_path, venue)
    coordinator.store_level(AutonomyLevel.MICRO)
    try:
        await coordinator.run_cycle([_opportunity()], evidence=_micro_evidence(), bankroll_usd=1000)
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE execution_gateway_requests SET created_at=? WHERE tier='execution'",
                (yesterday,),
            )
        result = await coordinator.reconcile()
    finally:
        coordinator.close()
        ledger.conn.close()
    assert result["cancelled"] == 1
    assert venue.cancelled
    with sqlite3.connect(path) as conn:
        status = conn.execute(
            "SELECT status FROM execution_gateway_requests WHERE tier='execution'"
        ).fetchone()
    assert status == ("cancelled",)


@pytest.mark.asyncio
async def test_halt_mid_cycle_stops_new_orders(tmp_path, monkeypatch) -> None:
    _live_flags(monkeypatch)
    venue = FakeVenue()
    venue.balance = "1000"
    venue.halt_after = 1
    _path, _gateway, ledger, coordinator = _open(tmp_path, venue)
    coordinator.store_level(AutonomyLevel.MICRO)
    now = datetime.now(UTC)
    try:
        report = await coordinator.run_cycle(
            [_opportunity("m1", now), _opportunity("m2", now)],
            evidence=_micro_evidence(),
            bankroll_usd=1000,
        )
    finally:
        coordinator.close()
        ledger.conn.close()
    assert venue.calls == 1
    assert report.submitted == 1
    assert report.halted is True


@pytest.mark.asyncio
async def test_halt_before_cycle_places_nothing(tmp_path, monkeypatch) -> None:
    _live_flags(monkeypatch)
    monkeypatch.setenv("NOEMA_MASTER_HALT", "1")
    venue = FakeVenue()
    venue.balance = "1000"
    _path, _gateway, ledger, coordinator = _open(tmp_path, venue)
    coordinator.store_level(AutonomyLevel.EXPANSION)
    try:
        report = await coordinator.run_cycle(
            [_opportunity()], evidence=_micro_evidence(), bankroll_usd=1000,
        )
    finally:
        coordinator.close()
        ledger.conn.close()
    assert venue.calls == 0
    assert report.submitted == 0
    assert report.paper_fills == 0
    assert report.halted is True


@pytest.mark.asyncio
async def test_edge_decay_closes_a_paper_position(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    venue = FakeVenue()
    path, _gateway, ledger, coordinator = _open(tmp_path, venue)
    opened = datetime.now(UTC)
    try:
        await coordinator.run_cycle(
            [_opportunity(when=opened, edge=0.36)],
            evidence=_paper_evidence(),
            bankroll_usd=2000,
        )
        await coordinator.run_cycle(
            [_opportunity(when=opened + timedelta(seconds=1), edge=0)],
            evidence=_paper_evidence(),
            bankroll_usd=2000,
        )
    finally:
        coordinator.close()
        ledger.conn.close()
    with sqlite3.connect(path) as conn:
        status = conn.execute(
            "SELECT status FROM trading_coordinator_positions"
        ).fetchone()
        exits = conn.execute(
            "SELECT value_state FROM economic_events WHERE event_type='prediction_exit'"
        ).fetchall()
    assert status == ("closed",)
    assert exits == [("paper",)]


def _snapshot(row, status, exposure, quantity, remaining, fill_ids, fee) -> ReconciliationObservation:
    return ReconciliationObservation(
        provider=row["venue"],
        proposal_id=row["proposal_id"],
        provider_reference=row["provider_reference"],
        status=status,
        observed_at=datetime.now(UTC),
        source="fake_venue",
        filled_quantity=quantity,
        fee_amount=fee,
        fee_currency="USD",
        external_fill_ids=tuple(fill_ids),
        filled_exposure_usd=exposure,
        remaining_reserved_usd=remaining,
    )


def test_console_reports_trading_coordinator_wired(monkeypatch) -> None:
    async def raw_wallets():
        return []

    async def value_wallets(networks):
        return networks

    monkeypatch.setattr("noema.dashboard_app.live_wallet_networks", raw_wallets)
    monkeypatch.setattr("noema.dashboard_app.value_native_wallets", value_wallets)
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.delenv("NOEMA_EXECUTION_GATEWAY_ENABLED", raising=False)
    monkeypatch.setenv("NOEMA_LIVE_REQUIRES_OWNER_REVIEW", "1")
    response = TestClient(app).get("/api/wallet-status?force=true")
    assert response.status_code == 200
    control = response.json()["control_plane"]
    assert control["trading_coordinator_wired"] is True
    assert control["coordinator_wired"] is False
    assert control["live_execution_enabled"] is False
    assert "COORDINATOR NOT WIRED" not in control["status"]
    assert control["status"].startswith("TRADING COORDINATOR WIRED")
    assert "PAPER DEFAULT" in control["status"]
