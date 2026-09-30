import csv
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from noema.economic_investigations import EconomicInvestigationStore
from noema.economic_ledger import DEFAULT_PROVIDER_PERIOD_MANIFEST, EconomicEvent, EconomicLedger
from noema.polymarket_owner_evidence import (
    ingest_polymarket_owner_export,
    inspect_polymarket_owner_export,
)


def _seed_existing(path, now):
    ledger = EconomicLedger(str(path))
    ledger.declare_period_manifest(month_utc="2026-09", providers=DEFAULT_PROVIDER_PERIOD_MANIFEST,
                                   provenance={"source": "test"})
    blockers = {
        "polymarket_us": "fees and settlement cashflows unknown",
        "wallets": "Base fee disputed", "kalshi": "refund coverage incomplete",
        "stripe": "September financial coverage incomplete", "openai": "provider billing unknown",
        "cloudflare": "billing unavailable", "render": "invoice allocation absent",
        "operator_expenses": "owner attestation absent",
    }
    for provider, blocker in blockers.items():
        ledger.attest_provider_coverage(
            provider=provider, month_utc="2026-09", state="PARTIAL",
            expected_evidence_classes=DEFAULT_PROVIDER_PERIOD_MANIFEST[provider],
            actual_evidence_classes=(), unresolved_blockers=[blocker],
            provenance={"source": "test"}, evidence={"fixture": True}, observed_at=now,
        )
    for ref, event_type, amount in (
        ("trade-1", "trade_observed", Decimal("2.00")),
        ("balance-10", "account_balance_change_observed", Decimal(10)),
        ("balance-15", "account_balance_change_observed", Decimal(15)),
    ):
        ledger.record_event(EconomicEvent(
            provider="polymarket_us", external_reference_id=ref,
            event_type=event_type, occurred_at=now, currency="USD", amount=amount,
            reconciliation_state="OBSERVED", value_state="unknown",
            capital_class="unclassified_cash", confidence_state="operator_reported",
            completeness_state="incomplete", evidence={"source": "existing official activity scan"},
        ))
    ledger.conn.close()


def _write_export(path):
    rows = [
        {"activity_id": "trade-1", "type": "ACTIVITY_TYPE_TRADE", "status": "COMPLETED",
         "timestamp": "2026-09-26T10:00:00Z", "amount": "2.00", "fee": "0.07",
         "currency": "USD", "opaque_note": "kept only in original source"},
        {"activity_id": "balance-10", "type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "status": "PENDING",
         "timestamp": "2026-09-27T10:00:00Z", "amount": "10.00", "currency": "USD"},
        {"activity_id": "balance-15", "type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "status": "COMPLETED",
         "timestamp": "2026-09-28T10:00:00Z", "amount": "15.00", "currency": "USD"},
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_owner_export_appends_evidence_matures_decision_and_reranks(tmp_path):
    now = datetime(2026, 9, 29, 18, tzinfo=UTC)
    db = tmp_path / "noema.db"
    _seed_existing(db, now)
    store = EconomicInvestigationStore(str(db))
    first = store.advance(month_utc="2026-09", now=now)
    first_id = first["decision_id"]
    original = store.conn.execute(
        "SELECT manifest_snapshot_json,alternatives_json,why FROM economic_investigation_decisions WHERE id=?",
        (first_id,),
    ).fetchone()
    store.conn.close()
    export = tmp_path / "polymarket-september.csv"
    _write_export(export)

    result = ingest_polymarket_owner_export(
        str(export), db_path=str(db), source_reference="owner local September export", now=now,
    )
    assert result["owner_input_decision_id"] == first_id
    assert result["maturation"] == "owned_by_normal_agent_cycle"
    store = EconomicInvestigationStore(str(db))
    assert store.latest(month_utc="2026-09")["decision_id"] == first_id
    assert store.latest(month_utc="2026-09")["last_decision_result"] == "AWAITING_OWNER_INPUT"
    second = store.advance(month_utc="2026-09", now=now)
    assert second["decision_id"] != first_id
    assert second["action"] in {"REQUEST_OWNER_INPUT", "INVESTIGATE", "WAIT", "RETRY_LATER", "PASS"}
    assert len(second["alternatives_considered"]) == 8
    assert store.conn.execute(
        "SELECT result_state FROM economic_investigation_results WHERE decision_id=? ORDER BY id DESC LIMIT 1",
        (first_id,),
    ).fetchone()[0] == "PARTIALLY_RESOLVED"
    store.conn.close()
    assert result["summary"]["matched_existing_events"] == 3
    assert result["summary"]["fee_rows"] == 1
    assert result["summary"]["final_balance_rows"] == 1
    assert result["summary"]["pending_balance_rows"] == 1
    assert second["period_closure"] == "OPEN"
    assert result["financial_authority_changed"] is False

    conn = sqlite3.connect(db)
    unchanged = conn.execute(
        "SELECT manifest_snapshot_json,alternatives_json,why FROM economic_investigation_decisions WHERE id=?",
        (first_id,),
    ).fetchone()
    assert tuple(original) == unchanged
    # Only the exact final $15 row reconciles. The pending $10 stays observed.
    groups = conn.execute(
        "SELECT source.external_reference_id,state.reconciliation_state FROM economic_events state "
        "JOIN economic_events source ON source.id=state.related_event_id "
        "WHERE state.provider='polymarket_us' AND state.event_type='reconciliation_state_update'"
    ).fetchall()
    assert [(ref, state) for ref, state in groups] == [
        ("balance-15", "RECONCILED")
    ]
    pending = conn.execute(
        "SELECT evidence_json FROM economic_events WHERE provider='polymarket_us' "
        "AND external_reference_id LIKE 'statement:%:balance-evidence'"
    ).fetchall()
    assert len(pending) == 1
    assert json.loads(pending[0][0])["row_facts"]["economic_origin"] == "unknown"
    fee = conn.execute(
        "SELECT evidence_json FROM economic_events WHERE provider='polymarket_us' "
        "AND event_type='statement_fee_observed'"
    ).fetchone()
    assert json.loads(fee[0])["row_facts"]["unknown_fields"] == ["opaque_note"]
    assert conn.execute(
        "SELECT COUNT(*) FROM polymarket_owner_evidence_imports"
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM economic_investigation_decisions WHERE month_utc='2026-09'").fetchone()[0] == 2
    conn.close()

    # Replaying the exact original file is idempotent: no duplicate evidence or decision.
    before_counts = sqlite3.connect(db).execute(
        "SELECT (SELECT COUNT(*) FROM economic_events),(SELECT COUNT(*) FROM economic_investigation_results),"
        "(SELECT COUNT(*) FROM economic_investigation_decisions)"
    ).fetchone()
    replay = ingest_polymarket_owner_export(str(export), db_path=str(db), now=now)
    after_counts = sqlite3.connect(db).execute(
        "SELECT (SELECT COUNT(*) FROM economic_events),(SELECT COUNT(*) FROM economic_investigation_results),"
        "(SELECT COUNT(*) FROM economic_investigation_decisions)"
    ).fetchone()
    assert replay["status"] == "duplicate_source"
    assert before_counts == after_counts


def test_unmatched_and_ambiguous_rows_do_not_claim_cashflow_or_resolution(tmp_path):
    now = datetime(2026, 9, 29, 18, tzinfo=UTC)
    db = tmp_path / "noema.db"
    _seed_existing(db, now)
    EconomicInvestigationStore(str(db)).advance(month_utc="2026-09", now=now)
    export = tmp_path / "ambiguous.json"
    export.write_text(json.dumps({"activities": [{
        "id": "unmatched", "type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT",
        "status": "unknown", "timestamp": "2026-09-28T10:00:00Z", "amount": "25.00",
        "currency": "USD", "private_memo": "not copied into the manifest",
    }]}), encoding="utf-8")
    result = ingest_polymarket_owner_export(str(export), db_path=str(db), now=now)
    assert result["summary"]["unmatched_rows"] == 1
    assert result["summary"]["pending_balance_rows"] == 1
    assert result["summary"]["final_balance_rows"] == 0
    store = EconomicInvestigationStore(str(db))
    same_decision = store.advance(month_utc="2026-09", now=now)
    assert same_decision["created_now"] is False
    assert store.conn.execute(
        "SELECT result_state FROM economic_investigation_results WHERE decision_id=? ORDER BY id DESC LIMIT 1",
        (result["owner_input_decision_id"],),
    ).fetchone()[0] == "BLOCKED"
    store.conn.close()
    conn = sqlite3.connect(db)
    event = conn.execute(
        "SELECT evidence_json FROM economic_events WHERE external_reference_id LIKE 'statement:%' LIMIT 1"
    ).fetchone()
    assert event is None
    assert conn.execute("SELECT COUNT(*) FROM economic_events WHERE provider='polymarket_us' AND event_type='reconciliation_state_update'").fetchone()[0] == 0
    conn.close()


def test_schema_inspection_is_value_free_and_does_not_touch_database(tmp_path):
    export = tmp_path / "export.csv"
    _write_export(export)
    inspected = inspect_polymarket_owner_export(str(export))
    assert inspected["status"] == "inspected_no_database_changes"
    assert inspected["rows_with_activity_id"] == 3
    assert inspected["rows_with_fee_field"] == 1
    assert inspected["rows_with_explicit_final_status"] == 2
    assert inspected["parseable_rows_by_utc_month"] == {"2026-09": 3}
    assert inspected["raw_values_returned"] is False
