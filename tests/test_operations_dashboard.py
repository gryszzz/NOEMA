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


def test_experiment_projection_links_persisted_runs_evidence_decisions_and_critic(tmp_path):
    path = tmp_path / "experiment-projection.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE research_trials(
                trial_id,family,hypothesis,feature_set_version,status,created_at,parent_trial_id,params_json
            );
            CREATE TABLE autonomous_research_runs(
                id,trial_id,specialist,kind,evidence_hash,worker_version,status,created_at,
                completed_at,elapsed_seconds,compute_cost_usd,result_json,evidence_path,mission_id
            );
            CREATE TABLE forecast_ledger(id,created_at,venue,market_id,forecast_json,action_json);
        """)
        conn.execute("INSERT INTO research_trials VALUES(?,?,?,?,?,?,?,?)", (
            "trial-a", "kalshi-history", "Check live history", "v1", "completed",
            NOW.isoformat(), None, json.dumps({"strategy_id": "strat-a", "market_id": "MKT-A"}),
        ))
        conn.executemany("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (1, "trial-a", "critic", "history", "hash-a", "v1", "completed",
             NOW.isoformat(), NOW.isoformat(), 1.0, None,
             json.dumps({"observations": 7, "critic_review": {"verdict": "PASS"}}), None, None),
            (2, "trial-a", "critic", "history", "", "v1", "completed",
             NOW.isoformat(), NOW.isoformat(), 1.0, None, json.dumps({"observations": 3}), None, None),
        ])
        conn.execute("INSERT INTO forecast_ledger VALUES(?,?,?,?,?,?)", (
            1, NOW.isoformat(), "kalshi", "MKT-A",
            json.dumps({"trial_id": "trial-a", "strategy_id": "strat-a"}),
            json.dumps({"decision": "pass"}),
        ))

    sections = build_operations(str(path), now=NOW)["sections"]
    experiment = sections["experiments"]["rows"][0]
    run = next(item for item in sections["research_runs"]["rows"] if item["id"] == 1)
    assert experiment["strategy_id"] == "strat-a"
    assert experiment["market_id"] == "MKT-A"
    assert experiment["run_count"] == 2
    assert experiment["evidence_count"] == 1
    assert experiment["decision_count"] == 1
    assert experiment["strategy_decision_count"] == 1
    assert run["observations"] == 7
    assert json.loads(run["result"])["critic_review"]["result_accepted"] is True


def test_sidecar_terminal_run_wins_over_incomplete_worker_copy(tmp_path):
    worker, sidecar = tmp_path / "worker.db", tmp_path / "console-state.db"
    schema = """CREATE TABLE autonomous_research_runs(
        id INTEGER PRIMARY KEY,trial_id TEXT,specialist TEXT,kind TEXT,evidence_hash TEXT,
        worker_version TEXT,status TEXT,created_at TEXT,completed_at TEXT,elapsed_seconds REAL,
        compute_cost_usd TEXT,result_json TEXT,evidence_path TEXT,mission_id TEXT)"""
    with sqlite3.connect(worker) as conn:
        conn.execute(schema)
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, "trial-a", "critic", "quality", "hash-a", "v1", "running",
            NOW.isoformat(), None, None, None, '{"observations":1}', None, None,
        ))
    with sqlite3.connect(sidecar) as conn:
        conn.execute(schema)
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            8, "trial-a", "critic", "quality", "hash-a", "v1", "completed",
            NOW.isoformat(), (NOW + timedelta(seconds=4)).isoformat(), 4.0, None,
            '{"observations":7}', None, None,
        ))

    runs = build_operations(str(worker), now=NOW, additional_paths=(str(sidecar),))["sections"]["research_runs"]["rows"]
    assert len(runs) == 1
    assert runs[0]["id"] == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["observations"] == 7


def test_sidecar_experiments_keep_metadata_and_relationship_projection(tmp_path):
    worker, sidecar = tmp_path / "worker.db", tmp_path / "console-state.db"
    with sqlite3.connect(worker) as conn:
        conn.execute("CREATE TABLE forecast_ledger(id,created_at,venue,market_id,forecast_json,action_json)")
        conn.execute("INSERT INTO forecast_ledger VALUES(?,?,?,?,?,?)", (
            1, NOW.isoformat(), "kalshi", "MKT-A",
            json.dumps({"trial_id":"trial-a", "strategy_id":"strategy-a"}),
            json.dumps({"decision":"pass"}),
        ))
    with sqlite3.connect(sidecar) as conn:
        conn.executescript("""
            CREATE TABLE research_trials(trial_id TEXT,family TEXT,hypothesis TEXT,
                feature_set_version TEXT,status TEXT,created_at TEXT,parent_trial_id TEXT,params_json TEXT);
            CREATE TABLE autonomous_research_runs(id,trial_id,specialist,kind,evidence_hash,
                worker_version,status,created_at,completed_at,elapsed_seconds,compute_cost_usd,
                result_json,evidence_path,mission_id);
            CREATE TABLE canonical_pair_observations(id,observed_at,canonical_event_id,
                canonical_proposition_id,semantic_status,settlement_equivalence,
                observation_hash,observation_json);
        """)
        conn.execute("INSERT INTO research_trials VALUES(?,?,?,?,?,?,?,?)", (
            "trial-a", "prediction", "test", "v1", "registered", NOW.isoformat(), None,
            json.dumps({"strategy_id":"strategy-a", "candidate_observation_hash":"obs-a"}),
        ))
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, "trial-a", "critic", "cross_venue_paper_experiment", "obs-a", "v1", "completed",
            NOW.isoformat(), NOW.isoformat(), 1.0, None, '{"observations":2}', None, None,
        ))
        conn.execute("INSERT INTO canonical_pair_observations VALUES(?,?,?,?,?,?,?,?)", (
            1, NOW.isoformat(), "event-a", "prop-a", "matched", "equivalent", "obs-a",
            json.dumps({"native_contracts":[{"market_id":"MKT-A"}]}),
        ))

    experiment = build_operations(
        str(worker), now=NOW, additional_paths=(str(sidecar),),
    )["sections"]["experiments"]["rows"][0]
    assert experiment["strategy_id"] == "strategy-a"
    assert experiment["market_id"] == "MKT-A"
    assert experiment["market_ids"] == ["MKT-A"]
    assert experiment["run_count"] == 1
    assert experiment["evidence_count"] == 1
    assert experiment["decision_count"] == 1
    assert experiment["strategy_decision_count"] == 1


def test_console_sidecar_records_are_included_in_existing_operation_sections(tmp_path):
    primary = tmp_path / "worker.db"
    sidecar = tmp_path / "console-state.db"
    with sqlite3.connect(primary) as conn:
        conn.execute("CREATE TABLE runtime_events(id,created_at,stage,status)")
    with sqlite3.connect(sidecar) as conn:
        conn.executescript("""
            CREATE TABLE research_trials(
                trial_id,family,hypothesis,feature_set_version,status,created_at,parent_trial_id
            );
            CREATE TABLE autonomous_research_runs(
                id,trial_id,specialist,kind,evidence_hash,worker_version,status,created_at,
                completed_at,elapsed_seconds,compute_cost_usd,result_json,evidence_path,mission_id
            );
            CREATE TABLE economic_events(
                id INTEGER PRIMARY KEY,created_at TEXT,event_type TEXT,amount_usd TEXT,payload_json TEXT
            );
        """)
        conn.execute("INSERT INTO research_trials VALUES(?,?,?,?,?,?,?)", (
            "sidecar-trial", "prediction", "Test hypothesis", "v1", "active", NOW.isoformat(), None,
        ))
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, "sidecar-trial", "critic", "validation", "evidence-1", "v1", "completed",
            NOW.isoformat(), NOW.isoformat(), 1.0, None, json.dumps({"observations": 4}), None, None,
        ))
        conn.execute("INSERT INTO economic_events VALUES(1,?,?,?,?)", (
            NOW.isoformat(), "wallet_transaction_confirmed", "12.50", "{}",
        ))

    sections = build_operations(str(primary), additional_paths=(str(sidecar),), now=NOW)["sections"]
    assert sections["experiments"]["rows"][0]["trial_id"] == "sidecar-trial"
    assert sections["research_runs"]["rows"][0]["observations"] == 4
    assert sections["wallet_transactions"]["rows"][0]["event_type"] == "wallet_transaction_confirmed"


def test_sidecar_terminal_run_wins_over_stale_incomplete_worker_record(tmp_path):
    primary = tmp_path / "worker.db"
    sidecar = tmp_path / "console-state.db"
    run_schema = """CREATE TABLE autonomous_research_runs(
        id INTEGER PRIMARY KEY,trial_id TEXT,specialist TEXT,kind TEXT,evidence_hash TEXT,
        worker_version TEXT,status TEXT,created_at TEXT,completed_at TEXT,elapsed_seconds REAL,
        compute_cost_usd TEXT,result_json TEXT,evidence_path TEXT,mission_id TEXT)"""
    with sqlite3.connect(primary) as conn:
        conn.execute("CREATE TABLE runtime_events(id,created_at,stage,status)")
        conn.execute(run_schema)
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, "trial", "critic", "validation", "hash", "v1", "running", NOW.isoformat(),
            None, None, None, json.dumps({"observations": 1}), None, None,
        ))
    with sqlite3.connect(sidecar) as conn:
        conn.execute(run_schema)
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            7, "trial", "critic", "validation", "hash", "v1", "completed", NOW.isoformat(),
            (NOW + timedelta(minutes=1)).isoformat(), 2.0, "0.3",
            json.dumps({"observations": 7, "critic_review": {"verdict": "PASS"}}), None, None,
        ))

    runs = build_operations(
        str(primary), additional_paths=(str(sidecar),), now=NOW,
    )["sections"]["research_runs"]["rows"]
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["observations"] == 7
    assert runs[0]["compute_cost_usd"] == "0.3"


def test_sidecar_only_experiment_uses_canonical_projection_relationships(tmp_path):
    primary = tmp_path / "worker.db"
    sidecar = tmp_path / "console-state.db"
    with sqlite3.connect(primary) as conn:
        conn.execute("CREATE TABLE runtime_events(id,created_at,stage,status)")
        conn.execute("CREATE TABLE forecast_ledger(id,forecast_json)")
        conn.execute("INSERT INTO forecast_ledger VALUES(1,?)", (
            json.dumps({"trial_id": "sidecar-trial", "strategy_id": "strategy-a"}),
        ))
    with sqlite3.connect(sidecar) as conn:
        conn.executescript("""
            CREATE TABLE research_trials(
                trial_id TEXT,family TEXT,hypothesis TEXT,feature_set_version TEXT,status TEXT,
                created_at TEXT,parent_trial_id TEXT,params_json TEXT,status_updated_at TEXT
            );
            CREATE TABLE autonomous_research_runs(
                id,trial_id,specialist,kind,evidence_hash,worker_version,status,created_at,
                completed_at,elapsed_seconds,compute_cost_usd,result_json,evidence_path,mission_id
            );
            CREATE TABLE canonical_pair_observations(observation_hash TEXT,observation_json TEXT);
        """)
        params = {"strategy_id": "strategy-a", "market_id": "MARKET-A",
                  "candidate_observation_hash": "observation-a"}
        conn.execute("INSERT INTO research_trials VALUES(?,?,?,?,?,?,?,?,?)", (
            "sidecar-trial", "prediction", "Hypothesis", "v1", "running", NOW.isoformat(),
            None, json.dumps(params), NOW.isoformat(),
        ))
        conn.execute("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, "sidecar-trial", "critic", "validation", "evidence-a", "v1", "completed",
            NOW.isoformat(), NOW.isoformat(), 1.0, None, "{}", "evidence/path", None,
        ))
        conn.execute("INSERT INTO canonical_pair_observations VALUES(?,?)", (
            "observation-a", json.dumps({"native_contracts": [{"market_id": "MARKET-A"}]}),
        ))

    experiments = build_operations(
        str(primary), additional_paths=(str(sidecar),), now=NOW,
    )["sections"]["experiments"]["rows"]
    row = next(item for item in experiments if item["trial_id"] == "sidecar-trial")
    assert row["params"] == params
    assert row["strategy_id"] == "strategy-a"
    assert row["market_id"] == "MARKET-A"
    assert row["market_ids"] == ["MARKET-A"]
    assert row["run_count"] == 1
    assert row["evidence_count"] == 1
    assert row["decision_count"] == 1


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
        for age, expected in ((10, "unknown"), (200, "stale"), (-10, "unknown")):
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
    assert runtime["state"] == "unknown"
    assert runtime["liveness"] == "UNKNOWN"
    assert runtime["health"] == "degraded"


def test_runtime_exposes_persisted_cycle_stage_timings(tmp_path):
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_runtime(agent_id,status_json)")
        conn.execute("INSERT INTO agent_runtime VALUES(?,?)", ("noema", json.dumps({
            "running": True,
            "last_heartbeat_at": NOW.isoformat(),
            "last_cycle": {"cycle_id": 374, "health": "healthy", "active_goal": "research", "cadence_seconds": 120},
        })))
        conn.execute("CREATE TABLE agent_cycle_timings(cycle_id,duration_seconds,stage_timings_json)")
        conn.execute("INSERT INTO agent_cycle_timings VALUES(?,?,?)", (
            374, 147.2, json.dumps({"market_collection": 121.3, "state_persistence": 0.2}),
        ))
    runtime = build_operations(str(path), now=NOW)["runtime"]
    assert runtime["cycle"]["duration_seconds"] == 147.2
    assert runtime["cycle"]["cadence_seconds"] == 120
    assert runtime["cycle"]["stage_timings"]["market_collection"] == 121.3


def test_stopped_agent_is_offline_even_with_a_recent_heartbeat(tmp_path):
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_runtime(agent_id,status_json)")
        conn.execute("INSERT INTO agent_runtime VALUES(?,?)", ("noema", json.dumps({
            "running": False,
            "last_heartbeat_at": NOW.isoformat(),
            "last_cycle": {"health": "degraded"},
        })))

    runtime = build_operations(str(path), now=NOW)["runtime"]

    assert runtime["state"] == "stopped"
    assert runtime["liveness"] == "STOPPED"


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
    assert result["groq"] == {"status": "not_configured", "credential_present": False}
    assert result["docker_model_runner"]["selected_model_resource_eligible"] is False
    assert "must never" not in serialized and "private" not in serialized
