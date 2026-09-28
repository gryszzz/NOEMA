import json
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from noema.dashboard_app import app
from noema.operations_dashboard import build_operations

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    result = build_operations(str(path), now=NOW)
    assert not path.exists()
    assert result["runtime"]["state"] == "unknown"
    assert all(s["status"] == "not_recorded" for s in result["sections"].values())


def test_bounded_read_only_records_and_private_json_excluded(tmp_path):
    path = tmp_path / "work.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE forecast_ledger(id,created_at,venue,market_id,forecast_json,action_json)")
        for i in range(60):
            conn.execute("INSERT INTO forecast_ledger VALUES(?,?,?,?,?,?)", (
                i, NOW.isoformat(), "paper", f"M{i}", json.dumps({"model_version": "v1", "private": "secret"}),
                json.dumps({"decision": "pass", "reason": "insufficient evidence", "private": "secret"}),
            ))
        conn.execute("CREATE TABLE research_trials(wrong_column)")
    before = path.read_bytes()
    result = build_operations(str(path), now=NOW)
    assert path.read_bytes() == before
    rows = result["sections"]["decisions"]
    assert rows["has_more"] and len(rows["rows"]) == 50
    assert rows["rows"][0]["id"] == 59
    assert rows["rows"][0]["decision"] == "pass"
    assert "secret" not in json.dumps(result)
    assert result["sections"]["experiments"]["status"] == "unavailable"


def test_runtime_requires_recent_nonfuture_heartbeat(tmp_path):
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_runtime(agent_id,status_json)")
        for age, expected in ((10, "running"), (200, "stale"), (-10, "unknown")):
            conn.execute("DELETE FROM agent_runtime")
            conn.execute("INSERT INTO agent_runtime VALUES(?,?)", ("noema", json.dumps({
                "running": True, "last_heartbeat_at": (NOW-timedelta(seconds=age)).isoformat(),
            })))
            conn.commit()
            assert build_operations(str(path), now=NOW)["runtime"]["state"] == expected


def test_corrupt_database_fails_explicitly(tmp_path):
    path = tmp_path / "broken.db"
    path.write_text("not a database")
    result = build_operations(str(path), now=NOW)
    assert result["runtime"]["state"] == "unavailable"
    assert all(s["status"] == "unavailable" for s in result["sections"].values())


def test_main_is_work_console_and_api_never_initializes_database(tmp_path, monkeypatch):
    path = tmp_path / "absent.db"
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    with TestClient(app) as client:
        response = client.get("/")
        assert 'Operational records' in response.text
        assert 'Observe · Infer · Verify · Act' not in response.text
        assert client.get("/static/operations.mjs").status_code == 200
        assert client.get("/api/operations").json()["database_present"] is False
        assert not path.exists()
        assert client.get("/detailed").status_code == 200
