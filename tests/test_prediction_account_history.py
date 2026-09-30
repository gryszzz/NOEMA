import sqlite3

from noema.prediction_account_history import (
    load_prediction_account_records,
    persist_prediction_account_records,
    prediction_account_sync_state,
)


def test_live_account_record_persistence_deduplicates_and_links_source_ids(tmp_path):
    database = tmp_path / "noema.db"
    snapshot = [{
        "venue": "kalshi",
        "coverage": {"positions_complete": True, "settlements_complete": True},
        "balance": {"observed_at": "2026-09-29T12:00:00Z", "cash_balance_usd": "9.00"},
        "positions": [{"ticker": "MKT-1", "position_fp": "0", "market_exposure_dollars": "0"}],
        "fills": [{"fill_id": "fill-1", "ticker": "MKT-1", "order_id": "order-1",
                    "count_fp": "1", "yes_price_dollars": "0.4"}],
        "orders": [{"order_id": "order-1", "ticker": "MKT-1", "status": "executed"}],
        "settlements": [{"ticker": "MKT-1", "settled_time": "2026-09-29T12:00:00Z",
                         "revenue": 100}],
    }]

    first = persist_prediction_account_records(database, snapshot, observed_at="2026-09-29T12:01:00Z")
    second = persist_prediction_account_records(database, snapshot, observed_at="2026-09-29T12:02:00Z")
    assert first["inserted"] == 5
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["skipped"] == 5

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT record_type,external_id,market_id,related_order_id,state "
            "FROM prediction_account_records ORDER BY record_type,external_id"
        ).fetchall()
    assert len(rows) == 5
    fill = next(row for row in rows if row[0] == "fill")
    assert fill[1:] == ("fill-1", "MKT-1", "order-1", None)
    position = next(row for row in rows if row[0] == "position")
    assert position[-1] == "settled"
    assert next(row for row in rows if row[0] == "settlement")[-1] == "settled"


def test_unchanged_immutable_activity_is_not_rewritten_but_mutable_order_state_is(tmp_path):
    database = tmp_path / "noema.db"
    first = [{"venue": "kalshi", "fills": [{"fill_id": "fill-1", "fee_cost": "0.01"}],
              "orders": [{"order_id": "order-1", "status": "resting"}]}]
    persist_prediction_account_records(database, first, observed_at="2026-09-29T12:00:00Z")
    second = [{"venue": "kalshi", "fills": [{"fill_id": "fill-1", "fee_cost": "0.01"}],
               "orders": [{"order_id": "order-1", "status": "executed"}]}]
    report = persist_prediction_account_records(database, second, observed_at="2026-09-29T12:01:00Z")
    assert report["updated"] == 1
    with sqlite3.connect(database) as connection:
        states = dict(connection.execute(
            "SELECT record_type,state FROM prediction_account_records WHERE venue='kalshi'"
        ))
        fill_observed = connection.execute(
            "SELECT last_observed_at FROM prediction_account_records "
            "WHERE venue='kalshi' AND record_type='fill'"
        ).fetchone()[0]
    assert states == {"fill": None, "order": "executed"}
    assert fill_observed == "2026-09-29T12:00:00Z"


def test_complete_position_scan_marks_disappeared_open_position_not_current(tmp_path):
    database = tmp_path / "noema.db"
    open_snapshot = [{"venue": "polymarket_us", "coverage": {"positions_complete": True},
                      "positions": [{"ticker": "market-a", "outcome": "Yes", "position_fp": "2"}]}]
    empty_snapshot = [{"venue": "polymarket_us", "coverage": {"positions_complete": True},
                       "positions": []}]
    persist_prediction_account_records(database, open_snapshot, observed_at="2026-09-29T12:00:00Z")
    persist_prediction_account_records(database, empty_snapshot, observed_at="2026-09-29T12:01:00Z")
    with sqlite3.connect(database) as connection:
        state = connection.execute(
            "SELECT state FROM prediction_account_records WHERE venue='polymarket_us' AND record_type='position'"
        ).fetchone()[0]
    assert state == "not_currently_open"


def test_sync_checkpoint_advances_only_after_complete_delta_and_full_audit(tmp_path):
    database = tmp_path / "noema.db"
    persist_prediction_account_records(database, [{
        "venue": "kalshi", "fills": [{"fill_id": "fill-1", "created_time": "2026-09-29T12:00:00Z"}],
        "sync_state": {"activity": {"success": True, "complete": True,
                                     "full_audit_complete": True,
                                     "high_water_at": "2026-09-29T12:00:00Z",
                                     "high_water_id": "fill-1"}},
    }], observed_at="2026-09-29T12:01:00Z")
    first = prediction_account_sync_state(database, "kalshi")["activity"]
    assert first["last_success_at"] == "2026-09-29T12:01:00Z"
    assert first["last_full_audit_at"] == "2026-09-29T12:01:00Z"
    assert first["high_water_id"] == "fill-1"

    persist_prediction_account_records(database, [{
        "venue": "kalshi", "fills": [{"fill_id": "fill-2", "created_time": "2026-09-29T12:02:00Z"}],
        "sync_state": {"activity": {"success": True, "complete": False,
                                     "full_audit_complete": False,
                                     "high_water_at": "2026-09-29T12:02:00Z",
                                     "high_water_id": "fill-2"}},
    }], observed_at="2026-09-29T12:03:00Z")
    after_gap = prediction_account_sync_state(database, "kalshi")["activity"]
    assert after_gap["last_success_at"] == "2026-09-29T12:01:00Z"
    assert after_gap["high_water_id"] == "fill-1"
    assert after_gap["last_full_audit_at"] == "2026-09-29T12:01:00Z"
    assert [row["fill_id"] for row in load_prediction_account_records(database, "kalshi")["fill"]] == ["fill-1", "fill-2"]


def test_complete_open_order_snapshot_closes_only_presence_not_reason(tmp_path):
    database = tmp_path / "noema.db"
    persist_prediction_account_records(database, [{
        "venue": "polymarket_us", "orders": [{"order_id": "order-1", "status": "OPEN"}],
    }], observed_at="2026-09-29T12:00:00Z")
    persist_prediction_account_records(database, [{
        "venue": "polymarket_us", "orders": [], "coverage": {"open_orders_complete": True},
    }], observed_at="2026-09-29T12:01:00Z")
    with sqlite3.connect(database) as connection:
        state = connection.execute(
            "SELECT state FROM prediction_account_records WHERE venue='polymarket_us' AND record_type='order'"
        ).fetchone()[0]
    assert state == "not_open_in_current_snapshot"
