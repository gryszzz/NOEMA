from datetime import UTC, datetime
from decimal import Decimal

from noema.economic_dashboard import build_economic_overview
from noema.economic_ledger import EconomicEvent, EconomicLedger
from noema.economic_models import EconomicSnapshot


def test_dashboard_reads_latest_economic_snapshot(tmp_path) -> None:
    path = str(tmp_path / "noema.db")
    ledger = EconomicLedger(path)
    ledger.append_snapshot(
        EconomicSnapshot(
            starting_capital_usd=Decimal(500),
            current_equity_usd=Decimal(600),
            realized_net_pnl_usd=Decimal(100),
            high_water_equity_usd=Decimal(550),
            reserve_usd=Decimal(200),
            strategy_capital_usd=Decimal(250),
            research_budget_usd=Decimal(50),
            infrastructure_budget_usd=Decimal(0),
            treasury_sweep_usd=Decimal(0),
        )
    )
    overview = build_economic_overview(path)
    assert overview["snapshot"]["current_equity_usd"] == "600"
    assert overview["profit_plan"]["profit_above_high_water_usd"] == "50"


def test_dashboard_projects_console_sidecar_events_without_writing_worker_snapshot(tmp_path) -> None:
    worker_path = tmp_path / "worker.db"
    console_path = tmp_path / "console-state.db"
    worker = EconomicLedger(str(worker_path))
    worker.append_snapshot(EconomicSnapshot(
        starting_capital_usd=Decimal(500), current_equity_usd=Decimal(600),
        realized_net_pnl_usd=Decimal(100), high_water_equity_usd=Decimal(600),
        reserve_usd=Decimal(200), strategy_capital_usd=Decimal(250),
        research_budget_usd=Decimal(50), infrastructure_budget_usd=Decimal(0),
        treasury_sweep_usd=Decimal(0),
    ))
    console = EconomicLedger(str(console_path))
    console.record_event(EconomicEvent(
        provider="kalshi", event_type="fill", occurred_at=datetime(2026, 9, 30, tzinfo=UTC),
        currency="USD", amount=Decimal(1), amount_usd=Decimal(1),
        reconciliation_state="RECONCILED", value_state="realized", capital_class="trading_pnl",
        confidence_state="provider_confirmed", completeness_state="complete",
        external_reference_id="sidecar-event",
    ))
    worker.conn.close()
    console.conn.close()
    worker_path.chmod(0o444)
    before = worker_path.read_bytes()

    overview = build_economic_overview(str(worker_path), additional_paths=(str(console_path),))

    assert overview["snapshot"]["current_equity_usd"] == "600"
    assert overview["canonical_ledger"]["event_count"] == 1
    assert worker_path.read_bytes() == before
