import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from noema.bill_tracker import BillTracker
from noema.cognition_store import CognitionStore
from noema.dashboard_app import app
from noema.economic_bootstrap import bootstrap_economy
from noema.economic_ledger import EconomicLedger
from noema.economic_measurement import build_economic_measurement

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def test_missing_database_stays_missing_and_profit_unknown(tmp_path, monkeypatch):
    path = tmp_path / 'missing' / 'noema.db'
    report = build_economic_measurement(str(path), now=NOW)
    assert report['status'] == 'no_recorded_cash'
    assert report['net_economic_profit_usd'] is None
    assert report['self_funding_demonstrated'] is False
    progress = report['mission_progress']
    assert progress['status'] == 'unproven'
    assert progress['verified_realized_revenue_usd'] is None
    assert progress['complete_attributable_costs_usd'] is None
    assert progress['reserve_balance_usd'] is None
    assert progress['self_funding_ratio'] is None
    assert not path.parent.exists()
    monkeypatch.setenv('NOEMA_DB_PATH', str(path))
    response = TestClient(app).get('/api/economic-measurement')
    assert response.status_code == 200
    assert not path.exists()


def test_measurement_includes_sidecar_only_research_run_cost(tmp_path):
    worker, sidecar = tmp_path / "worker.db", tmp_path / "console-state.db"
    with sqlite3.connect(worker) as conn:
        conn.execute("CREATE TABLE marker(value TEXT)")
    with sqlite3.connect(sidecar) as conn:
        conn.execute("""CREATE TABLE autonomous_research_runs(
            id INTEGER PRIMARY KEY,trial_id TEXT NOT NULL,specialist TEXT NOT NULL,
            kind TEXT NOT NULL,evidence_hash TEXT NOT NULL,worker_version TEXT NOT NULL,
            status TEXT NOT NULL,created_at TEXT NOT NULL,completed_at TEXT,deadline_at TEXT NOT NULL,
            elapsed_seconds REAL,compute_cost_usd TEXT,result_json TEXT,evidence_path TEXT,
            mission_id TEXT,UNIQUE(trial_id,evidence_hash,worker_version))""")
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, "trial-sidecar", "critic", "research", "hash-a", "v1", "completed",
            NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), 1.0, "0.42", "{}", None, None,
        ))
    report = build_economic_measurement(
        str(worker), now=NOW + timedelta(hours=1), additional_paths=(str(sidecar),),
    )
    row = next(item for item in report["activity_costs"]["rows"]
               if item["activity_id"] == "trial-sidecar")
    assert row["recorded_compute_cost_usd"] == "0.42"


def test_measurement_uses_completed_sidecar_run_once_when_worker_copy_is_stale(tmp_path, monkeypatch):
    worker, sidecar = tmp_path / "worker.db", tmp_path / "console-state.db"
    run_schema = """CREATE TABLE autonomous_research_runs(
        id INTEGER PRIMARY KEY,trial_id TEXT NOT NULL,specialist TEXT NOT NULL,
        kind TEXT NOT NULL,evidence_hash TEXT NOT NULL,worker_version TEXT NOT NULL,
        status TEXT NOT NULL,created_at TEXT NOT NULL,completed_at TEXT,deadline_at TEXT NOT NULL,
        elapsed_seconds REAL,compute_cost_usd TEXT,result_json TEXT,evidence_path TEXT,
        mission_id TEXT,UNIQUE(trial_id,evidence_hash,worker_version))"""
    values = ("trial-cost", "critic", "research", "hash-cost", "v1", "running",
              NOW.isoformat(), None, NOW.isoformat(), None, "0.20", "{}", None, None)
    with sqlite3.connect(worker) as conn:
        conn.execute(run_schema)
        conn.execute("INSERT INTO autonomous_research_runs VALUES(1," + ",".join("?" for _ in values) + ")",
                     values)
    completed_at = (NOW + timedelta(minutes=10)).isoformat()
    with sqlite3.connect(sidecar) as conn:
        conn.execute(run_schema)
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, *values[:5], "completed", values[6], completed_at, values[8], 2.0, "0.42",
            '{"observations":5}', None, None,
        ))

    report = build_economic_measurement(
        str(worker), now=NOW + timedelta(hours=1), additional_paths=(str(sidecar),),
    )
    rows = [item for item in report["activity_costs"]["rows"]
            if item["activity_id"] == "trial-cost"]
    assert len(rows) == 1
    assert rows[0]["recorded_compute_cost_usd"] == "0.42"
    monkeypatch.setenv("NOEMA_DB_PATH", str(worker))
    monkeypatch.setenv("NOEMA_CONSOLE_STATE_DB_PATH", str(sidecar))
    response = TestClient(app).get("/api/economic-measurement")
    assert response.status_code == 200
    api_row = next(item for item in response.json()["activity_costs"]["rows"]
                   if item["activity_id"] == "trial-cost")
    assert api_row["recorded_compute_cost_usd"] == "0.42"


def test_home_economic_api_exposes_period_closure_and_exact_provider_blockers(tmp_path, monkeypatch):
    path = str(tmp_path / 'noema.db')
    ledger = EconomicLedger(path)
    ledger.ensure_current_period_manifest(provenance={'source': 'api-test'})
    month = datetime.now(UTC).strftime('%Y-%m')
    monkeypatch.setenv('NOEMA_DB_PATH', path)
    response = TestClient(app).get('/api/economic-measurement')
    assert response.status_code == 200
    canonical = response.json()['canonical_ledger']
    assert canonical['period_closure'] == 'OPEN'
    assert canonical['coverage_status'] == 'PARTIAL'
    assert canonical['blocking_providers']
    assert all(row['month_utc'] == month for row in canonical['provider_coverage'])
    assert canonical['net_verified_contribution_usd'] is None
    ledger.conn.close()


def test_revenue_less_full_recorded_expenses_is_a_loss_and_estimates_are_not_double_counted(tmp_path):
    path = str(tmp_path / 'noema.db')
    tracker = BillTracker(path)
    tracker.record(kind='receipt', amount_usd=Decimal(100), source='customer', reference='r', now=NOW)
    tracker.record(kind='expense', amount_usd=Decimal(120), source='invoices', reference='e', now=NOW)
    tracker.record(kind='receipt', amount_usd=Decimal(999), source='future', reference='f',
                   now=NOW + timedelta(days=1))
    tracker.record(kind='receipt', amount_usd=Decimal(999), source='prior', reference='p',
                   now=NOW - timedelta(days=30))
    tracker.record(kind='expense', amount_usd=Decimal(3), source='pilot delivery',
                   reference='job-cost', activity_id='trial-1', now=NOW)
    tracker.record(kind='receipt', amount_usd=Decimal(10), source='processor payout',
                   reference='job-receipt', activity_id='trial-1', now=NOW)
    EconomicLedger(path).append_snapshot(bootstrap_economy(Decimal(10000)))
    CognitionStore(path).reserve_estimated_cost(2, daily_limit_usd=5, now=NOW)
    report = build_economic_measurement(path, now=NOW)
    assert report['cash']['net_cash_usd'] == '-13'
    assert report['cash']['entry_count'] == 4
    assert report['activity_cash']['rows'] == [{
        'activity_id': 'trial-1', 'recorded_receipts_usd': '10',
        'recorded_expenses_usd': '3', 'recorded_cash_net_usd': '7',
        'entry_count': 2, 'full_net_economic_profit_usd': None,
        'status': 'cash recorded; complete cost coverage not attested',
    }]
    assert report['operating_estimates']['model_reserved_usd'] == '2.0'
    assert report['status'] == 'recorded_cash_deficit'
    assert report['net_economic_profit_usd'] is None
    assert report['paper']['net_after_execution_costs_usd'] == '0'
    json.dumps(report, allow_nan=False)


def test_positive_cash_does_not_claim_self_funding_and_corrupt_values_are_visible(tmp_path):
    path = str(tmp_path / 'noema.db')
    tracker = BillTracker(path)
    tracker.record(kind='receipt', amount_usd=Decimal(100), source='customer', reference='r', now=NOW)
    report = build_economic_measurement(path, now=NOW)
    assert report['status'] == 'recorded_cash_surplus'
    assert report['self_funding_demonstrated'] is False
    assert report['mission_progress']['verified_realized_revenue_usd'] is None
    assert report['mission_progress']['known_operator_reported_cash_receipts_usd'] == '100'
    assert report['mission_progress']['status'] == 'unproven'
    tracker.conn.execute("UPDATE bill_entries SET amount_usd='NaN'")
    tracker.conn.commit()
    report = build_economic_measurement(path, now=NOW)
    assert report['status'] == 'records_invalid'
    assert report['invalid_accounting_records'] == 1
    json.dumps(report, allow_nan=False)


def test_failed_model_reservation_and_usage_estimate_link_to_research_trial(tmp_path):
    path = str(tmp_path / 'noema.db')
    store = CognitionStore(path)
    assert store.reserve_estimated_cost(
        .25, daily_limit_usd=1, activity_id='session-1', now=NOW,
    )
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE cognitive_sessions (
            session_id TEXT, created_at TEXT, result_json TEXT,
            estimated_model_cost_usd REAL, compute_cost_usd REAL)""")
        conn.execute("INSERT INTO cognitive_sessions VALUES (?,?,?,?,?)", (
            'session-1', NOW.isoformat(), json.dumps({'trial_id': 'trial-1'}), .12, None,
        ))
    report = build_economic_measurement(path, now=NOW)
    row = report['activity_costs']['rows'][0]
    assert row['activity_id'] == 'trial-1'
    assert row['estimated_model_cost_usd'] == '0.12'
    assert row['model_budget_reserved_usd'] == '0.25'
    assert row['recorded_compute_cost_usd'] is None
    assert row['compute_cost_status'] == 'unknown'
    assert row['full_net_economic_profit_usd'] is None
