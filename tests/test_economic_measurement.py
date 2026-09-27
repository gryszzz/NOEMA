import json
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
    assert not path.parent.exists()
    monkeypatch.setenv('NOEMA_DB_PATH', str(path))
    response = TestClient(app).get('/api/economic-measurement')
    assert response.status_code == 200
    assert not path.exists()


def test_revenue_less_full_recorded_expenses_is_a_loss_and_estimates_are_not_double_counted(tmp_path):
    path = str(tmp_path / 'noema.db')
    tracker = BillTracker(path)
    tracker.record(kind='receipt', amount_usd=Decimal(100), source='customer', reference='r', now=NOW)
    tracker.record(kind='expense', amount_usd=Decimal(120), source='invoices', reference='e', now=NOW)
    tracker.record(kind='receipt', amount_usd=Decimal(999), source='future', reference='f',
                   now=NOW + timedelta(days=1))
    tracker.record(kind='receipt', amount_usd=Decimal(999), source='prior', reference='p',
                   now=NOW - timedelta(days=30))
    EconomicLedger(path).append_snapshot(bootstrap_economy(Decimal(10000)))
    CognitionStore(path).reserve_estimated_cost(2, daily_limit_usd=5, now=NOW)
    report = build_economic_measurement(path, now=NOW)
    assert report['cash']['net_cash_usd'] == '-20'
    assert report['cash']['entry_count'] == 2
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
    tracker.conn.execute("UPDATE bill_entries SET amount_usd='NaN'")
    tracker.conn.commit()
    report = build_economic_measurement(path, now=NOW)
    assert report['status'] == 'records_invalid'
    assert report['invalid_accounting_records'] == 1
    json.dumps(report, allow_nan=False)
