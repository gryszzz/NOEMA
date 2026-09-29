from datetime import UTC, datetime
from decimal import Decimal

from noema.economic_ledger import EconomicEvent, EconomicLedger
from noema.economic_models import EconomicSnapshot


def test_economic_snapshot_roundtrip(tmp_path) -> None:
    ledger = EconomicLedger(str(tmp_path / "noema.db"))
    snapshot = EconomicSnapshot(
        starting_capital_usd=Decimal(500),
        current_equity_usd=Decimal(550),
        realized_net_pnl_usd=Decimal(50),
        high_water_equity_usd=Decimal(550),
        reserve_usd=Decimal(200),
        strategy_capital_usd=Decimal(200),
        research_budget_usd=Decimal(75),
        infrastructure_budget_usd=Decimal(25),
        treasury_sweep_usd=Decimal(0),
    )
    ledger.append_snapshot(snapshot)
    assert ledger.latest_snapshot() == snapshot


def event(**changes):
    values = {
        "provider": "test-provider", "event_type": "cash_receipt",
        "external_reference_id": "event-1", "occurred_at": datetime(2026, 9, 29, tzinfo=UTC),
        "currency": "USD", "amount": Decimal(10), "amount_usd": Decimal(10),
        "reconciliation_state": "RECONCILED", "value_state": "realized",
        "capital_class": "revenue", "confidence_state": "provider_confirmed",
        "completeness_state": "complete", "lane": "api",
    }
    values.update(changes)
    return EconomicEvent(**values)


def test_provider_events_deduplicate_and_conflicts_are_append_only(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    first = event()
    inserted = ledger.record_event(first)
    duplicate = ledger.record_event(first)
    assert inserted["status"] == "inserted"
    assert duplicate == {"event_id": inserted["event_id"], "status": "duplicate"}
    conflict = ledger.record_event(event(amount=Decimal(12), amount_usd=Decimal(12)))
    assert conflict["status"] == "disputed"
    assert ledger.conn.execute("SELECT COUNT(*) FROM economic_events WHERE provider='test-provider'").fetchone()[0] == 2
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"))
    assert projection["net_verified_contribution_usd"] is None
    assert projection["unreconciled_event_count"] >= 1


def test_same_provider_event_can_gain_reconciliation_without_rewriting_source(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    observed = event(reconciliation_state="OBSERVED", confidence_state="estimated",
                     completeness_state="incomplete")
    inserted = ledger.record_event(observed)
    reconciled = ledger.record_event(event())
    assert reconciled["status"] == "state_update"
    source = ledger.conn.execute(
        "SELECT reconciliation_state,confidence_state,completeness_state FROM economic_events WHERE id=?",
        (inserted["event_id"],),
    ).fetchone()
    assert tuple(source) == ("OBSERVED", "estimated", "incomplete")
    assert ledger.record_event(event())["status"] == "duplicate"


def test_owner_funding_is_not_revenue_and_owned_transfers_are_excluded(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    ledger.record_event(event(
        provider="wallet:base", event_type="owner_deposit", external_reference_id="deposit-1",
        capital_class="owner_capital", amount=Decimal(50), amount_usd=Decimal(50),
    ))
    ledger.record_event(event(
        provider="wallet:base", event_type="internal_transfer", external_reference_id="transfer-1",
        capital_class="transfer", amount=Decimal(8), amount_usd=Decimal(8),
        owned_account_id="treasury-a", counterparty_account_id="treasury-b",
    ))
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"))
    assert projection["verified_realized_revenue_usd"] is None
    assert projection["owner_funded_subtotal_usd"] == "50"
    assert projection["unclassified_cash_subtotal_usd"] == "0"


def test_reconciliation_after_fee_and_refund_preserves_history(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    fill = ledger.record_event(event(
        provider="kalshi", event_type="fill", external_reference_id="fill-1",
        capital_class="trading_pnl", reconciliation_state="OBSERVED",
        confidence_state="estimated", completeness_state="incomplete",
    ))
    fee = ledger.record_event(event(
        provider="kalshi", event_type="fee", external_reference_id="fee-1",
        capital_class="cost", amount=Decimal("0.25"), amount_usd=Decimal("0.25"),
    ))
    before = ledger.conn.execute("SELECT COUNT(*) FROM economic_events").fetchone()[0]
    ledger.append_reconciliation(fill["event_id"], state="RECONCILED", evidence={"provider_fill": "confirmed"})
    ledger.append_reconciliation(fee["event_id"], state="RECONCILED", evidence={"provider_fee": "confirmed"})
    ledger.record_event(event(
        provider="stripe", event_type="refund", external_reference_id="refund-1",
        amount=Decimal(-3), amount_usd=Decimal(-3), capital_class="revenue",
    ))
    assert ledger.conn.execute("SELECT COUNT(*) FROM economic_events").fetchone()[0] == before + 3
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"))
    assert projection["verified_attributable_costs_usd"] is None
    assert projection["reconciled_cost_subtotal_usd"] == "0.25"
    assert projection["reconciled_trading_pnl_subtotal_usd"] == "10"
    assert projection["net_verified_contribution_usd"] is None


def test_paper_results_and_reservations_never_become_realized_profit(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    ledger.record_event(event(
        provider="paper", event_type="paper_result", external_reference_id="paper-1",
        amount=Decimal(100), amount_usd=Decimal(100), capital_class="none", value_state="paper",
    ))
    ledger.record_event(event(
        provider="openai", event_type="model_cost_reservation", external_reference_id="reserve-1",
        amount=Decimal(1), amount_usd=Decimal(1), capital_class="cost", value_state="reservation",
        reconciliation_state="ESTIMATED", confidence_state="estimated", completeness_state="incomplete",
    ))
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"))
    assert projection["verified_realized_revenue_usd"] is None
    assert projection["verified_realized_trading_pnl_usd"] is None
    assert projection["reserved_amount_usd"] == "1"
    assert projection["unreconciled_amount_usd"] == "0"


def test_missing_cost_coverage_keeps_net_and_self_funding_unknown(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    ledger.record_event(event())
    ledger.attest_provider_coverage(provider="stripe", month_utc="2026-09", state="COMPLETE")
    ledger.attest_provider_coverage(provider="openai", month_utc="2026-09", state="INCOMPLETE")
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"), month_utc="2026-09")
    assert projection["verified_realized_revenue_usd"] is None
    assert projection["verified_attributable_costs_usd"] is None
    assert projection["net_verified_contribution_usd"] is None
    assert projection["self_funding_ratio"] is None


def test_complete_coverage_allows_verified_net_and_self_funding_projection(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    ledger.record_event(event())
    ledger.record_event(event(
        provider="hosting", event_type="hosting_cost", external_reference_id="host-1",
        amount=Decimal(4), amount_usd=Decimal(4), capital_class="cost", lane="infrastructure",
    ))
    for provider in ("test-provider", "hosting"):
        ledger.attest_provider_coverage(provider=provider, month_utc="2026-09", state="COMPLETE")
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"), month_utc="2026-09")
    assert projection["verified_realized_revenue_usd"] == "10"
    assert projection["verified_attributable_costs_usd"] == "4"
    assert projection["net_verified_contribution_usd"] == "6"
    assert projection["self_funding_ratio"] == "2.5"
