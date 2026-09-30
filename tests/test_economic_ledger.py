from datetime import UTC, datetime
from decimal import Decimal

import pytest

from noema.economic_ledger import EconomicCounterfactual, EconomicEvent, EconomicLedger
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


def test_read_projection_merges_console_sidecar_events_without_duplicates(tmp_path):
    primary = EconomicLedger(str(tmp_path / "worker.db"))
    sidecar = EconomicLedger(str(tmp_path / "console-state.db"))
    primary.record_event(event(external_reference_id="worker-event"))
    sidecar.record_event(event(external_reference_id="console-event", event_type="fee",
                               capital_class="cost", amount=Decimal(-2),
                               amount_usd=Decimal(-2)))

    projection = EconomicLedger.read_projection(
        str(tmp_path / "worker.db"), additional_paths=(str(tmp_path / "console-state.db"),),
    )
    assert projection["event_count"] == 2

    # A matching event copied into both databases is represented only once.
    sidecar.record_event(event(external_reference_id="worker-event"))
    projection = EconomicLedger.read_projection(
        str(tmp_path / "worker.db"), additional_paths=(str(tmp_path / "console-state.db"),),
    )
    assert projection["event_count"] == 2


def test_read_projection_uses_sidecar_when_worker_replica_is_missing(tmp_path):
    sidecar_path = tmp_path / "console-state.db"
    sidecar = EconomicLedger(str(sidecar_path))
    sidecar.record_event(event(external_reference_id="durable-sidecar-event"))

    projection = EconomicLedger.read_projection(
        str(tmp_path / "missing-worker.db"), additional_paths=(str(sidecar_path),),
    )

    assert projection["event_count"] == 1


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
    ledger.attest_provider_coverage(provider="stripe", month_utc="2026-09", state="COMPLETE",
        expected_evidence_classes=["captured-payments"], actual_evidence_classes=["captured-payments"],
        provenance={"source": "fixture"})
    ledger.attest_provider_coverage(provider="openai", month_utc="2026-09", state="PARTIAL",
        expected_evidence_classes=["invoice-costs"], actual_evidence_classes=["usage"],
        unresolved_blockers=["admin cost scope unavailable"], provenance={"source": "fixture"})
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
        ledger.attest_provider_coverage(provider=provider, month_utc="2026-09", state="COMPLETE",
            expected_evidence_classes=["cash"], actual_evidence_classes=["cash"],
            provenance={"source": "fixture"})
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"), month_utc="2026-09")
    assert projection["verified_realized_revenue_usd"] == "10"
    assert projection["verified_attributable_costs_usd"] == "4"
    assert projection["net_verified_contribution_usd"] == "6"
    assert projection["self_funding_ratio"] == "2.5"
    assert projection["period_closure"] == "CLOSED"


def test_manifest_requires_every_expected_provider_and_evidence_class(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    ledger.declare_period_manifest(month_utc="2026-09", providers={
        "stripe": ["captured-payment", "refunds"],
        "openai": ["usage", "invoice-cost"],
    }, provenance={"manifest": "fixture-v1"})
    ledger.attest_provider_coverage(
        provider="stripe", month_utc="2026-09", state="COMPLETE",
        actual_evidence_classes=["captured-payment", "refunds"],
        provenance={"source": "stripe-read-only-export"},
    )
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"), month_utc="2026-09")
    assert projection["period_closure"] == "OPEN"
    assert projection["coverage_status"] == "PARTIAL"
    assert projection["blocking_providers"] == ["openai"]
    assert projection["net_verified_contribution_usd"] is None
    stripe_coverage = next(row for row in projection["provider_coverage"] if row["provider"] == "stripe")
    assert stripe_coverage["expected_evidence_classes"] == [
        "captured-payment", "refunds",
    ]
    with pytest.raises(ValueError):
        ledger.attest_provider_coverage(
            provider="stripe", month_utc="2026-09", state="COMPLETE",
            actual_evidence_classes=["captured-payment"], provenance={"source": "fixture"},
        )


def test_manifest_closes_only_after_provider_complete_or_explicitly_not_applicable(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    ledger.declare_period_manifest(month_utc="2026-09", providers={
        "stripe": ["captured-payment"], "kalshi": ["fills", "fees", "settlements"],
    }, provenance={"manifest": "fixture-v1"})
    ledger.attest_provider_coverage(
        provider="stripe", month_utc="2026-09", state="COMPLETE",
        actual_evidence_classes=["captured-payment"], provenance={"source": "fixture"},
    )
    ledger.attest_provider_coverage(
        provider="kalshi", month_utc="2026-09", state="NOT_APPLICABLE",
        evidence={"reason": "research-only venue; no live positions/orders"},
        provenance={"verified_by": "read-only account snapshot"},
    )
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"), month_utc="2026-09")
    assert projection["period_closure"] == "CLOSED"
    assert projection["coverage_complete"] is True
    assert projection["net_verified_contribution_usd"] == "0"


def test_default_period_manifest_is_idempotent_and_silence_never_closes(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    first = ledger.ensure_current_period_manifest(provenance={"source": "test"})
    count = ledger.conn.execute("SELECT COUNT(*) FROM economic_provider_coverage").fetchone()[0]
    second = ledger.ensure_current_period_manifest(provenance={"source": "test"})
    assert first and second == []
    assert ledger.conn.execute("SELECT COUNT(*) FROM economic_provider_coverage").fetchone()[0] == count
    month = datetime.now(UTC).strftime("%Y-%m")
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"), month_utc=month)
    assert projection["period_closure"] == "OPEN"
    assert len(projection["blocking_providers"]) == len(first)


def test_wallet_usd_valuation_requires_event_time_quote_provenance(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    with pytest.raises(ValueError, match="event-time price provenance"):
        ledger.record_event(event(provider="wallet:base", currency="ETH_wei", amount=Decimal(10**18),
            amount_usd=Decimal(2000), capital_class="unclassified_wallet_flow"))
    valued = event(provider="wallet:base", event_type="balance_valuation", currency="ETH_wei",
        amount=Decimal(10**18), amount_usd=Decimal(2000), capital_class="unclassified_wallet_flow",
        evidence={"decimals": 18, "price_source": "fixture-oracle", "price_timestamp":
            "2026-09-29T00:00:00+00:00", "valuation_basis": "event_time", "price_usd": "2000"})
    ledger.record_event(valued)


def test_authoritative_invoice_reconciliation_appends_adjustment_not_rewrite(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    estimate = event(provider="render", event_type="hosting_estimate", external_reference_id="render-estimate-2026-09",
        amount=Decimal("7.25"), amount_usd=Decimal("7.25"), capital_class="cost",
        reconciliation_state="ESTIMATED", confidence_state="estimated", completeness_state="incomplete")
    original = ledger.record_event(estimate)["event_id"]
    ids = ledger.reconcile_event_to_amount(
        original, authoritative_amount=Decimal("8.12"),
        authoritative_amount_usd=Decimal("8.12"),
        evidence={"record_type": "provider_invoice", "invoice_reference": "invoice-2026-09"},
        occurred_at=datetime(2026, 9, 29, 2, tzinfo=UTC),
    )
    original_row = ledger.conn.execute(
        "SELECT amount_usd,reconciliation_state FROM economic_events WHERE id=?", (original,),
    ).fetchone()
    assert tuple(original_row) == ("7.25", "ESTIMATED")
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"))
    assert projection["reconciled_cost_subtotal_usd"] == "8.12"
    assert len(ids) == 2


def test_authoritative_billing_expense_requires_noema_scope_and_provenance(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    recorded = ledger.record_authoritative_billing_expense(
        provider="render", external_reference_id="invoice-ref-2026-09",
        month_utc="2026-09", amount_usd=Decimal("4.25"),
        occurred_at=datetime(2026, 9, 30, tzinfo=UTC), service_scope="noema_service",
        source="provider_invoice", document_reference="secure-invoice-store:sha256:abc",
        attribution_basis="invoice line item identifies the NOEMA service",
    )
    assert recorded["status"] == "inserted"
    row = ledger.conn.execute(
        "SELECT provider,event_type,amount_usd,reconciliation_state,capital_class,evidence_json "
        "FROM economic_events WHERE id=?", (recorded["event_id"],),
    ).fetchone()
    assert row[0:5] == (
        "render", "authoritative_billing_expense", "4.25", "RECONCILED", "cost",
    )
    assert '"shared_workspace_cost_included": false' in row[5]
    with pytest.raises(ValueError, match="explicitly attributed"):
        ledger.record_authoritative_billing_expense(
            provider="render", external_reference_id="shared-invoice",
            month_utc="2026-09", amount_usd=Decimal(20),
            occurred_at=datetime(2026, 9, 30, tzinfo=UTC), service_scope="shared_workspace",
            source="provider_invoice", document_reference="secure-invoice-store:sha256:def",
            attribution_basis="whole workspace total",
        )


def test_provider_projection_keeps_estimates_and_wallet_aliases_truthful(tmp_path):
    path = str(tmp_path / "events.db")
    ledger = EconomicLedger(path)
    ledger.declare_period_manifest(
        month_utc="2026-09",
        providers={"render": ["invoice"], "wallets": ["activity"], "kalshi": ["settlement"]},
        provenance={"source": "test manifest"},
    )
    ledger.record_event(event(
        provider="render", event_type="hosting_estimate", external_reference_id="render-estimate",
        amount=Decimal("7.25"), amount_usd=Decimal("7.25"), capital_class="cost",
        reconciliation_state="ESTIMATED", value_state="unknown", confidence_state="estimated",
        completeness_state="incomplete",
    ))
    ledger.attest_provider_coverage(
        provider="render", month_utc="2026-09", state="PARTIAL", evidence={"invoice": "missing"},
        unresolved_blockers=["invoice missing"], applicable_activity_detected=None,
    )
    ledger.record_event(event(
        provider="wallet:base", event_type="chain_fee", external_reference_id="tx-fee",
        currency="wei", amount=Decimal(123), amount_usd=None, capital_class="cost",
        reconciliation_state="DISPUTED", confidence_state="provider_confirmed",
        completeness_state="incomplete",
    ))
    ledger.attest_provider_coverage(
        provider="wallets", month_utc="2026-09", state="PARTIAL", evidence={"chain": "base"},
        unresolved_blockers=["other chain history missing"], applicable_activity_detected=True,
    )
    ledger.record_event(event(
        provider="kalshi", event_type="settlement_pnl", external_reference_id="settlement-pnl",
        amount=Decimal(-1), amount_usd=Decimal(-1), capital_class="trading_pnl",
    ))
    ledger.record_event(event(
        provider="kalshi", event_type="fee", external_reference_id="settlement-fee",
        amount=Decimal("0.25"), amount_usd=Decimal("0.25"), capital_class="cost",
    ))
    ledger.attest_provider_coverage(
        provider="kalshi", month_utc="2026-09", state="PARTIAL", evidence={"settlement": "observed"},
        unresolved_blockers=["refund history missing"], applicable_activity_detected=True,
    )
    coverage = {item["provider"]: item for item in EconomicLedger.read_projection(
        path, month_utc="2026-09",
    )["provider_coverage"]}
    assert coverage["render"]["canonical_event_count"] == 1
    assert coverage["render"]["applicable_activity_detected"] is None
    assert coverage["render"]["estimated_amount_usd"] == "7.25"
    assert coverage["wallets"]["canonical_event_count"] == 1
    assert coverage["wallets"]["applicable_activity_detected"] is True
    assert coverage["wallets"]["reconciled_amount_usd"] is None
    assert coverage["kalshi"]["reconciled_amount_usd"] == "-1.25"


def test_reserve_stays_unknown_without_complete_ownership_balances_and_liabilities(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    result = ledger.attest_operating_reserve(
        month_utc="2026-09", observed_at=datetime(2026, 9, 29, tzinfo=UTC),
        designated_accounts=["phantom"], balances=[], liabilities_usd=None,
        reservations_usd=None, liabilities_complete=False, reservations_complete=False,
        provenance={"source": "fixture"},
    )
    assert result["state"] == "INCOMPLETE"
    assert result["operating_reserve_usd"] is None


def test_counterfactual_scenarios_are_persisted_separately_from_financial_events(tmp_path):
    ledger = EconomicLedger(str(tmp_path / "events.db"))
    ledger.predeclare_mission_counterfactuals(
        mission_id="mission-1", declared_at=datetime(2026, 9, 29, tzinfo=UTC),
        evidence={"method": "owner-approved-mission-plan"},
    )
    for scenario_type in ("actual_action", "do_nothing", "baseline_strategy", "alternative_allocation"):
        ledger.append_counterfactual(EconomicCounterfactual(
            scenario_id=f"mission-1:{scenario_type}", mission_id="mission-1",
            scenario_type=scenario_type, outcome_basis="simulation",
            recorded_at=datetime(2026, 9, 29, 1, tzinfo=UTC), amount_usd=Decimal(2),
            evidence={"method": "bounded_fixture"},
        ))
    projection = EconomicLedger.read_projection(str(tmp_path / "events.db"), month_utc="2026-09")
    assert len(projection["counterfactual_comparisons"]) == 4
    assert projection["event_count"] == 0
    assert projection["net_verified_contribution_usd"] is None
    with pytest.raises(ValueError):
        ledger.append_counterfactual(EconomicCounterfactual(
            scenario_id="invalid", mission_id="mission-1", scenario_type="execute_now",
            outcome_basis="simulation", recorded_at=datetime(2026, 9, 29, tzinfo=UTC),
        ))
