"""QA regressions: dashboard handlers must not block the event loop; economic measurement must not
load research result payloads into memory."""
import inspect
import sqlite3

from noema import dashboard_app
from noema.economic_measurement import build_economic_measurement

SYNC_BUILDER_ROUTES = ["agent", "doctor", "ladder", "trench_desks", "overview", "economy", "ecosystem",
                       "economic_measurement", "bill", "radar", "trench", "wallet_policy"]


def test_sync_builder_routes_run_in_threadpool() -> None:
    for name in SYNC_BUILDER_ROUTES:
        assert not inspect.iscoroutinefunction(getattr(dashboard_app, name)), name


def test_command_center_offloads_desk_overview() -> None:
    src = inspect.getsource(dashboard_app.command_center)
    assert "to_thread(desk_overview" in src


def test_economic_measurement_does_not_select_result_payloads(tmp_path, monkeypatch) -> None:
    db = tmp_path / "noema.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE autonomous_research_runs (trial_id TEXT, evidence_hash TEXT, worker_version TEXT,"
              " status TEXT, created_at TEXT, completed_at TEXT, compute_cost_usd TEXT, result_json TEXT)")
    c.execute("INSERT INTO autonomous_research_runs VALUES ('t','h','v','completed',datetime('now'),"
              "datetime('now'),'0.5',?)", ("x" * 1000,))
    c.commit()
    c.close()
    seen: list[str] = []
    real_connect = sqlite3.connect

    def connect(*a, **k):
        conn = real_connect(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    out = build_economic_measurement(str(db))
    assert out
    run_sql = [s for s in seen if "FROM autonomous_research_runs" in s and "SELECT" in s]
    assert run_sql and all("CASE WHEN trim(coalesce(result_json" in s for s in run_sql)
