import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from noema.cognition_dashboard import build_provider_health
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


def test_home_reads_recorded_economics_and_attention_without_writing(tmp_path):
    path = tmp_path / "economics.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE economic_snapshots(id,created_at,snapshot_json)")
        conn.execute("INSERT INTO economic_snapshots VALUES(?,?,?)", (1, NOW.isoformat(), json.dumps({
            "reserve_usd": "25", "strategy_capital_usd": "20",
            "research_budget_usd": "5", "infrastructure_budget_usd": "3",
        })))
        conn.execute("CREATE TABLE ecosystem_reviews(id,created_at,dominant_specialist,idle_fraction,payload_json)")
        conn.execute("INSERT INTO ecosystem_reviews VALUES(?,?,?,?,?)", (
            1, NOW.isoformat(), "quant", .2,
            json.dumps({"idle_fraction": .2, "allocations": [
                {"specialist": "quant", "family": "prediction_markets", "state": "paper",
                 "attention_fraction": .8, "reason": "measured evidence"},
            ]}),
        ))
    before = path.read_bytes()
    result = build_operations(str(path), now=NOW)
    assert path.read_bytes() == before
    assert result["economic_state"] == {
        "status": "recorded", "created_at": NOW.isoformat(),
        "balances": {"reserve_usd": "25", "strategy_capital_usd": "20",
                     "research_budget_usd": "5", "infrastructure_budget_usd": "3"},
    }
    assert result["attention_allocations"]["rows"][0]["attention_fraction"] == .8
    assert result["attention_allocations"]["rows"][0]["attention_delta"] is None


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


def test_economic_lanes_are_broad_read_only_and_never_infer_revenue(tmp_path):
    path = tmp_path / "lanes.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE research_trials(trial_id,family)")
        conn.executemany("INSERT INTO research_trials VALUES(?,?)", [
            ("prediction-trial", "prediction_markets_data_quality"),
            ("api-trial", "api_usage_validation"),
        ])
        conn.execute("CREATE TABLE autonomous_research_runs(trial_id,kind,specialist)")
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?)", (
            "prediction-trial", "market_data_quality", "kalshi-history",
        ))
        conn.execute("CREATE TABLE ecosystem_specialists(name,family)")
        conn.execute("INSERT INTO ecosystem_specialists VALUES(?,?)", ("trench-1", "solana_new_tokens"))
    before = path.read_bytes()
    result = build_operations(str(path), now=NOW)
    assert path.read_bytes() == before
    lanes = {lane["key"]: lane for lane in result["economic_lanes"]["lanes"]}
    assert len(lanes) == 10
    assert lanes["prediction"]["trial_count"] == 1
    assert lanes["prediction"]["run_count"] == 1
    assert lanes["web3"]["specialist_count"] == 1
    assert lanes["apis"]["trial_count"] == 1
    for lane in lanes.values():
        assert lane["receipts_usd"] is None
        assert lane["verified_expenses_usd"] is None
        assert lane["realized_net_usd"] is None
        assert lane["financial_status"] == "unmeasured"


def test_mission_measurements_keep_unknowns_unknown_and_attribute_exact_sources(tmp_path):
    path = tmp_path / "mission-measurements.db"
    mission_id = "mission-a"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE research_trials(trial_id,family);
            CREATE TABLE missions(mission_id,trial_id,evidence_hash,session_id,run_id,objective,status,
                specialist,capability_grants_json,resource_grant_json,result_json,lesson_id,
                created_at,updated_at,completed_at);
            CREATE TABLE autonomous_research_runs(id,trial_id,specialist,kind,status,elapsed_seconds,
                compute_cost_usd,result_json,mission_id);
            CREATE TABLE cognitive_sessions(session_id,created_at,completed_at,provider,model,
                estimated_model_cost_usd,compute_cost_usd);
            CREATE TABLE mission_handoffs(id,mission_id,created_at,from_specialist,to_specialist,
                objective,status,result_json);
            CREATE TABLE bill_entries(id,activity_id,kind,amount_usd,source,reference,created_at);
            CREATE TABLE ecosystem_reviews(id,created_at,payload_json);
            CREATE TABLE mission_events(id,mission_id,created_at,actor,event_type,status,detail,payload_json);
        """)
        conn.execute("INSERT INTO research_trials VALUES(?,?)", ("trial-a", "prediction_markets_data_quality"))
        conn.execute("INSERT INTO missions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            mission_id, "trial-a", "hash", "session-a", 7, "Check data quality", "completed",
            "kalshi-history", "[]", "{}", json.dumps({"observations": 4, "valid_markets": 3,
                "critic_review": {"verdict": "PASS", "result_accepted": True}}), 3,
            NOW.isoformat(), NOW.isoformat(), NOW.isoformat(),
        ))
        conn.execute("INSERT INTO missions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "mission-pass", "trial-a", "other-hash", None, None, "Avoid unsupported repeat", "passed",
            "kalshi-history", "[]", "{}", json.dumps({"status": "passed"}), None,
            NOW.isoformat(), (NOW+timedelta(seconds=3)).isoformat(),
            (NOW+timedelta(seconds=3)).isoformat(),
        ))
        conn.execute("INSERT INTO mission_events VALUES(?,?,?,?,?,?,?,?)", (
            1, "mission-pass", (NOW+timedelta(seconds=3)).isoformat(), "NOEMA",
            "lesson_applied", "passed", "Applied persisted lesson", "{}",
        ))
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?)", (
            7, "trial-a", "kalshi-history", "market_data_quality", "completed", 1.25, None,
            json.dumps({"observations": 4, "valid_markets": 3,
                "critic_review": {"verdict": "PASS", "result_accepted": True}}), mission_id,
        ))
        conn.execute("INSERT INTO cognitive_sessions VALUES(?,?,?,?,?,?,?)", (
            "session-a", (NOW-timedelta(seconds=8)).isoformat(), NOW.isoformat(),
            "deterministic", "evidence-priority-v1", None, None,
        ))
        conn.execute("INSERT INTO mission_handoffs VALUES(?,?,?,?,?,?,?,?)", (
            1, mission_id, NOW.isoformat(), "kalshi-history", "evidence-critic",
            "Review the result", "completed", json.dumps({"verdict": "PASS"}),
        ))
        conn.execute("INSERT INTO bill_entries VALUES(?,?,?,?,?,?,?)", (
            1, "session-a", "expense", "0.25", "operator", "one recorded expense", NOW.isoformat(),
        ))
        conn.execute("INSERT INTO bill_entries VALUES(?,?,?,?,?,?,?)", (
            2, "other-session", "receipt", "99", "operator", "unrelated", NOW.isoformat(),
        ))
        for review_id, created, share in ((1, NOW + timedelta(seconds=1), .4),
                                          (2, NOW + timedelta(seconds=2), .2)):
            payload = {"allocations": [{
                "specialist": "kalshi-history", "attention_fraction": share,
            }]}
            if review_id == 2:
                payload["mission_review"] = {
                    "mission_id": mission_id, "specialist": "kalshi-history",
                    "outcome": "DECREASE", "basis": "Measured critic rejection",
                    "previous_attention_fraction": .4, "current_attention_fraction": .2,
                }
            conn.execute("INSERT INTO ecosystem_reviews VALUES(?,?,?)", (
                review_id, created.isoformat(), json.dumps(payload),
            ))
    before = path.read_bytes()
    missions = build_operations(str(path), now=NOW)["sections"]["missions"]["rows"]
    mission = next(row for row in missions if row["mission_id"] == mission_id)
    measured = mission["measurement"]
    assert path.read_bytes() == before
    assert measured["economic_lane"] == "prediction"
    assert measured["elapsed_worker_seconds"] == 1.25
    assert measured["elapsed_session_seconds"] == 8
    assert measured["model_api_cost_usd"] is None
    assert measured["compute_cost_usd"] is None
    assert measured["data_provider_cost_usd"] is None
    assert measured["experiment_total_cost_usd"] is None
    assert measured["cash_receipts_usd"] == "0"
    assert measured["cash_expenses_usd"] == "0.25"
    assert measured["fees_usd"] is None
    assert measured["full_net_economic_profit_usd"] is None
    assert measured["information_outcome"]["critic_verdict"] == "PASS"
    assert measured["allocation_follow_up"]["attention_delta"] == -.2
    assert measured["allocation_follow_up"]["outcome"] == "DECREASE"
    assert measured["allocation_follow_up"]["review_id"] == 2
    assert [item["specialist"] for item in measured["specialist_contributions"]] == [
        "kalshi-history", "NOEMA cognition", "evidence-critic",
    ]
    assert measured["paper_outcome"]["status"] == "not_applicable"
    passed = next(row for row in missions if row["mission_id"] == "mission-pass")
    assert passed["measurement"]["specialist_contributions"] == [{
        "specialist": "NOEMA", "role": "lesson-based coordination", "status": "passed",
        "event": "lesson_applied", "elapsed_seconds": None, "cost_usd": None,
    }]


def test_recent_heartbeat_exposes_degraded_cycle_health(tmp_path):
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_runtime(agent_id,status_json)")
        conn.execute("INSERT INTO agent_runtime VALUES(?,?)", ("noema", json.dumps({
            "running": True,
            "last_heartbeat_at": NOW.isoformat(),
            "last_cycle": {"health": "degraded", "active_goal": "bounded research"},
        })))
    runtime = build_operations(str(path), now=NOW)["runtime"]
    assert runtime["state"] == "running"
    assert runtime["health"] == "degraded"


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


def test_provider_health_projection_is_storeless_and_secret_free(monkeypatch):
    monkeypatch.setattr(
        "noema.cognition_dashboard.cognition_config_from_env",
        lambda: SimpleNamespace(ready=True),
    )
    monkeypatch.setattr(
        "noema.cognition_dashboard.cognition_provider_name",
        lambda _config: "cloudflare_workers_ai",
    )
    monkeypatch.setattr("noema.cognition_dashboard._runtime_providers", lambda _config: {
        "hosted_providers": {
            "cloudflare_workers_ai": {"status": "healthy", "model": "fixture",
                                       "model_available": True, "credential_present": True,
                                       "api_token": "must never leave provider config"},
            "openai": {"status": "ready", "credential_present": True,
                       "api_key": "must never leave provider config"},
        },
        "local_model_runner": {"status": "healthy", "selected_model": "fixture-model",
                                "selected_model_resource_eligible": False,
                                "selected_model_resource_reason": "RESOURCE LIMITED",
                                "model_size_ceiling_gib": 1.5, "models": ["private inventory"]},
        "specialists": {"chronos": {"status": "unavailable", "endpoint": "private"},
                        "finbert": {"status": "healthy", "endpoint": "private"}},
    })
    result = build_provider_health()
    serialized = json.dumps(result)
    assert result["configured_provider"] == "cloudflare_workers_ai"
    assert result["cloudflare"]["status"] == "healthy"
    assert result["openai"]["status"] == "ready"
    assert result["docker_model_runner"]["selected_model_resource_eligible"] is False
    assert "must never" not in serialized and "private" not in serialized
