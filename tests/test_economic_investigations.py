import sqlite3
from datetime import UTC, datetime, timedelta

from noema.economic_investigations import EconomicInvestigationStore
from noema.economic_ledger import DEFAULT_PROVIDER_PERIOD_MANIFEST, EconomicLedger


def _open_september(path, now):
    ledger = EconomicLedger(str(path))
    ledger.declare_period_manifest(
        month_utc="2026-09", providers=DEFAULT_PROVIDER_PERIOD_MANIFEST,
        provenance={"source": "test economic manifest"},
    )
    blockers = {
        "polymarket_us": (["fees and settlement cashflows unknown"], {
            "activity_records_scanned": 61, "september_trades": 54,
            "september_position_resolutions": 4, "september_account_balance_changes": 2,
            "trade_fee_field_present": False, "eof_reached": True,
        }),
        "wallets": (["Base fee evidence disputed"], {"native_fee_dispute": True}),
        "kalshi": (["refund/void/correction coverage incomplete"], {"explicit_refund_void_feed_available": False}),
        "stripe": (["fees/refunds/payouts unavailable"], {"missing_read_capabilities": ["list_refunds"]}),
        "openai": (["provider cost estimate is not reconciled"], {"admin_api_key_present": False}),
        "cloudflare": (["billing API HTTP 403; usage unknown"], {"billing_api_http_status": 403}),
        "render": (["authoritative invoice/allocation absent"], {"provider_invoice_or_statement_present": False}),
        "operator_expenses": (["owner attestation absent"], {"owner_no_expenses_attestation_present": False}),
    }
    for provider, (items, evidence) in blockers.items():
        ledger.attest_provider_coverage(
            provider=provider, month_utc="2026-09", state="PARTIAL",
            expected_evidence_classes=DEFAULT_PROVIDER_PERIOD_MANIFEST[provider],
            actual_evidence_classes=(), observed_at=now,
            unresolved_blockers=items, provenance={"source": "test source"}, evidence=evidence,
        )
    ledger.conn.close()


def test_september_decision_prioritizes_polymarket_and_records_all_alternatives(tmp_path):
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    path = tmp_path / "economy.db"
    _open_september(path, now)
    store = EconomicInvestigationStore(str(path))
    decision = store.advance(month_utc="2026-09", now=now)

    assert decision["action"] == "REQUEST_OWNER_INPUT"
    assert decision["selected_candidate"] == "polymarket_us"
    assert "Official September Polymarket US" in decision["owner_input_required"]
    assert decision["expected_information_value"] == 0.96
    assert decision["expected_cost_usd"] == 0.0
    assert decision["period_closure"] == "OPEN"
    assert decision["financial_authority_changed"] is False
    assert decision["priority_change_reason"] == "Initial persisted autonomous ranking for the open period."
    assert {row["candidate_id"] for row in decision["alternatives_considered"]} == {
        "polymarket_us", "wallets", "kalshi", "stripe", "openai", "cloudflare", "render", "operator_expenses",
    }
    cloudflare = next(row for row in decision["alternatives_considered"] if row["candidate_id"] == "cloudflare")
    assert cloudflare["action"] == "RETRY_LATER"
    assert "near-zero expected information value" in cloudflare["rationale"]
    assert decision["next_eligible_retry"] == (now + timedelta(days=7)).isoformat()
    assert decision["selected_assessment"]["evidence_age_seconds"] == 0
    assert decision["selected_assessment"]["autonomous_action_permitted"] is False
    assert decision["last_decision_result"] == "AWAITING_OWNER_INPUT"
    assert store.latest(month_utc="2026-09")["result_history"][0]["state"] == "AWAITING_OWNER_INPUT"
    assert {item["lifecycle_state"] for item in store.latest(month_utc="2026-09")["result_history"]} >= {
        "PROPOSED", "EVALUATED", "OWNER_INPUT_REQUIRED",
    }

    again = store.advance(month_utc="2026-09", now=now + timedelta(minutes=1))
    assert again["decision_id"] == decision["decision_id"]
    assert again["created_now"] is False
    assert store.conn.execute("SELECT COUNT(*) FROM economic_investigation_decisions").fetchone()[0] == 1

    coverage = EconomicLedger.read_projection(str(path), month_utc="2026-09")
    assert coverage["period_closure"] == "OPEN"
    assert coverage["net_verified_contribution_usd"] is None
    assert coverage["event_count"] == 0


def test_blocker_change_advances_decision_and_no_new_evidence_is_not_failure(tmp_path):
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    path = tmp_path / "economy.db"
    _open_september(path, now)
    store = EconomicInvestigationStore(str(path))
    first = store.advance(month_utc="2026-09", now=now)
    store.record_result(
        first["decision_id"], result_state="NO_NEW_EVIDENCE",
        detail="Current read-only activity snapshot still lacks fee and balance-flow fields.",
        evidence={"source": "existing provider manifest"}, recorded_at=now + timedelta(minutes=5),
    )
    assert store.latest(month_utc="2026-09")["last_decision_result"] == "NO_NEW_EVIDENCE"

    ledger = EconomicLedger(str(path))
    ledger.attest_provider_coverage(
        provider="polymarket_us", month_utc="2026-09", state="PARTIAL",
        expected_evidence_classes=DEFAULT_PROVIDER_PERIOD_MANIFEST["polymarket_us"],
        actual_evidence_classes=("fills",), observed_at=now + timedelta(hours=1),
        unresolved_blockers=["settlement cashflows unknown"],
        provenance={"source": "new read-only evidence"},
        evidence={"activity_records_scanned": 62, "trade_fee_field_present": False},
    )
    ledger.conn.close()
    second = store.advance(month_utc="2026-09", now=now + timedelta(hours=1))
    assert second["decision_id"] != first["decision_id"]
    assert second["selected_candidate"] == "polymarket_us"
    assert second["last_decision_result"] == "AWAITING_OWNER_INPUT"
    store.conn.close()


def test_unrelated_manifest_refresh_does_not_repeat_selected_owner_request(tmp_path):
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    path = tmp_path / "economy.db"
    _open_september(path, now)
    store = EconomicInvestigationStore(str(path))
    first = store.advance(month_utc="2026-09", now=now)

    ledger = EconomicLedger(str(path))
    ledger.attest_provider_coverage(
        provider="stripe", month_utc="2026-09", state="PARTIAL",
        expected_evidence_classes=DEFAULT_PROVIDER_PERIOD_MANIFEST["stripe"],
        actual_evidence_classes=(), observed_at=now + timedelta(minutes=5),
        unresolved_blockers=["fees/refunds/payouts unavailable"],
        provenance={"source": "test refresh"},
        evidence={"missing_read_capabilities": ["list_refunds"], "snapshot_id": "new"},
    )
    ledger.conn.close()

    again = store.advance(month_utc="2026-09", now=now + timedelta(minutes=6))
    assert again["decision_id"] == first["decision_id"]
    assert again["created_now"] is False
    assert again["last_decision_result"] == "AWAITING_OWNER_INPUT"
    assert store.conn.execute("SELECT COUNT(*) FROM economic_investigation_decisions").fetchone()[0] == 1
    store.conn.close()


def test_unchanged_blocker_becomes_eligible_after_cooldown(tmp_path):
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    path = tmp_path / "economy.db"
    _open_september(path, now)
    store = EconomicInvestigationStore(str(path))
    first = store.advance(month_utc="2026-09", now=now)

    retried = store.advance(month_utc="2026-09", now=now + timedelta(days=7, minutes=1))
    assert retried["decision_id"] != first["decision_id"]
    assert retried["selected_candidate"] == "polymarket_us"
    assert retried["next_eligible_retry"] == (now + timedelta(days=14, minutes=1)).isoformat()
    store.conn.close()


def test_decision_snapshot_is_immutable_and_results_append_maturation_review(tmp_path):
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    path = tmp_path / "economy.db"
    _open_september(path, now)
    store = EconomicInvestigationStore(str(path))
    decision = store.advance(month_utc="2026-09", now=now)
    original = store.conn.execute(
        "SELECT manifest_snapshot_json,alternatives_json,why FROM economic_investigation_decisions WHERE id=?",
        (decision["decision_id"],),
    ).fetchone()
    assert decision["decision_state_at_time"]["provider_coverage"]
    assert decision["authority_state"].startswith("decision_only")
    assert decision["rejected_alternatives"]
    assert decision["selected_assessment"]["selection_status"] == "SELECTED"

    with sqlite3.connect(path) as direct:
        try:
            direct.execute(
                "UPDATE economic_investigation_decisions SET why='rewritten' WHERE id=?",
                (decision["decision_id"],),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("decision-time record must reject mutation")

    # A newer provider observation is appended as maturation/review evidence.
    ledger = EconomicLedger(str(path))
    ledger.attest_provider_coverage(
        provider="polymarket_us", month_utc="2026-09", state="PARTIAL",
        expected_evidence_classes=DEFAULT_PROVIDER_PERIOD_MANIFEST["polymarket_us"],
        actual_evidence_classes=("fills",), observed_at=now + timedelta(hours=1),
        unresolved_blockers=["settlement cashflows unknown"],
        provenance={"source": "new authoritative provider observation"},
        evidence={"activity_records_scanned": 62, "trade_fee_field_present": False},
    )
    ledger.conn.close()
    next_decision = store.advance(month_utc="2026-09", now=now + timedelta(hours=1))
    assert next_decision["decision_id"] != decision["decision_id"]

    history = store.latest(month_utc="2026-09")["decision_history"][1]["result_history"]
    states = [item["lifecycle_state"] for item in history]
    assert "MATURED" in states and "REVIEWED" in states
    assert all(item["actual_resources"]["cost_status"] == "unknown"
               for item in history if item["lifecycle_state"] in {"MATURED", "REVIEWED"})
    unchanged = store.conn.execute(
        "SELECT manifest_snapshot_json,alternatives_json,why FROM economic_investigation_decisions WHERE id=?",
        (decision["decision_id"],),
    ).fetchone()
    assert tuple(original) == tuple(unchanged)
    store.conn.close()


def test_result_records_preserve_links_cost_outcome_counterfactual_and_calibration(tmp_path):
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    path = tmp_path / "economy.db"
    _open_september(path, now)
    store = EconomicInvestigationStore(str(path))
    decision = store.advance(month_utc="2026-09", now=now)
    store.record_result(
        decision["decision_id"], result_state="STARTED", detail="Read-only evidence audit started.",
        lifecycle_state="STARTED", recorded_at=now + timedelta(minutes=1),
        evidence={"source": "provider manifest"}, actual_resources={"provider_calls": 0},
        actual_outcome={"status": "in_progress"}, links={"mission_id": "mission-1", "trial_id": "trial-1"},
    )
    store.record_result(
        decision["decision_id"], result_state="REVIEWED", detail="Audit reviewed; cashflow remains unknown.",
        lifecycle_state="REVIEWED", recorded_at=now + timedelta(minutes=2),
        measured_benefit={"amount_usd": None, "status": "unknown"},
        counterfactual={"available": False}, calibration={"status": "not_assessable"},
    )
    latest = store.latest(month_utc="2026-09")
    reviewed = latest["result_history"][0]
    assert reviewed["lifecycle_state"] == "REVIEWED"
    assert reviewed["measured_benefit"] == {"amount_usd": None, "status": "unknown"}
    started = latest["result_history"][1]
    assert started["mission_id"] == "mission-1"
    assert started["trial_id"] == "trial-1"
    assert started["actual_resources"] == {"provider_calls": 0}
    store.conn.close()
