from datetime import UTC, datetime
from decimal import Decimal
from sqlite3 import IntegrityError

import pytest
from fastapi.testclient import TestClient

from noema.bill_tracker import BillTracker
from noema.cognition_store import CognitionStore
from noema.dashboard_app import app


def test_bill_tracker_keeps_paper_capital_out_and_catches_duplicate_entries(tmp_path, monkeypatch):
    db = str(tmp_path / "noema.db")
    tracker = BillTracker(db)
    now = datetime(2026, 9, 26, tzinfo=UTC)
    assert tracker.overview(now=now)["status"] == "estimate_missing"
    tracker.configure(hosting_usd=Decimal(7), other_usd=Decimal(1),
                      owner_limit_usd=Decimal(5))
    assert tracker.overview(now=now)["status"] == "over_owner_limit"
    tracker.record(kind="receipt", amount_usd=Decimal(4), source="settled proceeds",
                   reference="payout-01", now=now)
    tracker.record(kind="expense", amount_usd=Decimal(2), source="provider invoice",
                   reference="invoice-01", now=now)
    with pytest.raises(IntegrityError):
        tracker.record(kind="receipt", amount_usd=Decimal(4), source="settled proceeds",
                       reference="payout-01", now=now)
    overview = tracker.overview(now=now)
    assert overview["uncovered_estimate_usd"] == "4"
    assert overview["status"] == "within_owner_limit"
    assert tracker.overview(now=datetime(2026, 10, 1, tzinfo=UTC))["recorded_receipts_usd"] == "0"
    monkeypatch.setenv("NOEMA_DB_PATH", db)
    assert TestClient(app).get("/api/bill").status_code == 200


def test_model_reservation_respects_monthly_budget_across_days(tmp_path):
    store = CognitionStore(str(tmp_path / "noema.db"))
    for day in (1, 2):
        assert store.reserve_estimated_cost(
            0.4, daily_limit_usd=1, monthly_limit_usd=1,
            now=datetime(2026, 9, day, tzinfo=UTC),
        )
    assert not store.reserve_estimated_cost(
        0.3, daily_limit_usd=1, monthly_limit_usd=1,
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )
    assert store.reserve_estimated_cost(
        0.3, daily_limit_usd=1, monthly_limit_usd=1,
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_legacy_entries_migrate_and_cash_can_be_attributed_to_one_job(tmp_path):
    import sqlite3

    path = tmp_path / "noema.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE bill_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, month_utc TEXT NOT NULL,
            kind TEXT NOT NULL, amount_usd TEXT NOT NULL, source TEXT NOT NULL,
            reference TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)""")
        conn.execute("""INSERT INTO bill_entries VALUES
            (1,'2026-09','expense','1.25','provider invoice','legacy-1',
             '2026-09-26T00:00:00+00:00')""")
    tracker = BillTracker(str(path))
    tracker.record(kind="receipt", amount_usd=Decimal(4), source="processor payout",
                   reference="receipt-1", activity_id="trial-123",
                   now=datetime(2026, 9, 26, tzinfo=UTC))
    row = tracker.conn.execute("SELECT activity_id FROM bill_entries WHERE reference='receipt-1'").fetchone()
    assert row == ("trial-123",)
    assert tracker.conn.execute("SELECT activity_id FROM bill_entries WHERE reference='legacy-1'").fetchone() == (None,)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0.001"])
def test_bill_tracker_rejects_invalid_amounts(tmp_path, amount):
    tracker = BillTracker(str(tmp_path / "noema.db"))
    with pytest.raises(ValueError):
        tracker.configure(hosting_usd=Decimal(amount), other_usd=Decimal(0),
                          owner_limit_usd=Decimal(10))
