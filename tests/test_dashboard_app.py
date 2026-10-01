import asyncio
import gzip
import os
import sqlite3
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from noema.dashboard_app import _capital_sample_sleep_seconds, _runtime_change_stream, app


def test_capital_sampler_targets_start_to_start_interval() -> None:
    assert _capital_sample_sleep_seconds(15, 100.0, now=102.5) == 12.5
    assert _capital_sample_sleep_seconds(15, 100.0, now=115.0) == 0
    assert _capital_sample_sleep_seconds(15, 100.0, now=117.0) == 0


def test_capital_sampler_forces_live_reads_and_writes_console_history(monkeypatch, tmp_path) -> None:
    from noema import dashboard_app

    database = tmp_path / "worker.db"
    database.touch()
    state_path = tmp_path / "console-state.db"
    calls = []

    monkeypatch.setattr(dashboard_app, "_db_path", lambda: str(database))
    monkeypatch.setattr(dashboard_app, "_console_state_db_path", lambda: str(state_path))

    async def wallets(*, force=False):
        calls.append(("wallets", force))
        return {"networks": [], "observed_at": "wallet-observed-at"}

    async def venues(*, force=False):
        calls.append(("venues", force))
        return {"venues": []}

    monkeypatch.setattr(dashboard_app, "wallet_status", wallets)
    monkeypatch.setattr(dashboard_app, "prediction_venues", venues)
    monkeypatch.setattr(dashboard_app, "stripe_economy_overview", lambda path: {"status": "not_observed"})

    def persist(path, venue_state, wallet_state, *, window, stripe):
        calls.append(("persist", path, venue_state, wallet_state, window, stripe))

    monkeypatch.setattr(dashboard_app, "balance_history", persist)

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def stop_after_cycle(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(dashboard_app.asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(dashboard_app.asyncio, "sleep", stop_after_cycle)

    try:
        asyncio.run(dashboard_app._sample_capital_history())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("sampler should have been cancelled after one completed cycle")

    assert calls[:2] == [("wallets", True), ("venues", True)]
    persisted = calls[2]
    assert persisted[0:2] == ("persist", str(state_path))
    assert persisted[4] == "24H"
    assert persisted[3]["observed_at"] == "wallet-observed-at"


def test_dashboard_root_renders_console() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "NOEMA · Autonomous economy" in response.text
    assert "Operational records" in response.text
    assert client.get("/static/brand/noema-face.png").status_code == 200


def test_overview_endpoint_is_safe_without_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "missing.db"))
    client = TestClient(app)
    response = client.get("/api/overview")
    assert response.status_code == 200
    assert response.json()["database_present"] is False


def test_ecosystem_endpoint_is_safe_without_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "missing-ecosystem.db"))
    client = TestClient(app)
    response = client.get("/api/ecosystem")
    assert response.status_code == 200
    assert response.json()["database_present"] is False


def test_execution_gateway_endpoint_is_read_only_and_fail_closed_without_database(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "missing-gateway.db"
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    response = TestClient(app).get("/api/execution-gateway")
    assert response.status_code == 200
    assert response.json()["status"] == "FAIL CLOSED · NOT ARMED"
    assert response.json()["recent_requests"] == []
    assert not path.exists()


def test_prediction_venues_endpoint_returns_read_only_status(monkeypatch) -> None:
    async def fake_status(*, force=False):
        return {"execution_enabled": False, "venues": [], "cross_venue_comparison": {"matches": []}}

    monkeypatch.setattr("noema.dashboard_app.build_prediction_venue_status", fake_status)
    response = TestClient(app).get("/api/prediction-venues")
    assert response.status_code == 200
    assert response.json()["execution_enabled"] is False


def test_wallet_refresh_applies_native_asset_valuation_before_projection(monkeypatch) -> None:
    async def raw_wallets():
        return [{"chain": "solana", "address": "wallet", "readable": True, "sol": "2"}]

    async def value_wallets(networks):
        return [{**networks[0], "native_value_usd": "300.00",
                 "native_valuation": {"source": "test spot quote", "price_usd": "150"}}]

    monkeypatch.setattr("noema.dashboard_app.live_wallet_networks", raw_wallets)
    monkeypatch.setattr("noema.dashboard_app.value_native_wallets", value_wallets)
    response = TestClient(app).get("/api/wallet-status?force=true")
    assert response.status_code == 200
    network = response.json()["networks"][0]
    assert network["native_value_usd"] == "300.00"
    assert network["native_valuation"]["source"] == "test spot quote"


def test_stripe_projection_endpoint_is_read_only_and_safe_without_database(monkeypatch, tmp_path) -> None:
    path = tmp_path / "missing-stripe.db"
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    response = TestClient(app).get("/api/stripe-economy")
    assert response.status_code == 200
    assert response.json()["status"] == "not_observed"
    assert not path.exists()


def test_runtime_stream_tracks_snapshot_replacement_and_console_state(monkeypatch, tmp_path) -> None:
    from noema.console_state import console_state_db_path
    from noema.prediction_account_history import persist_prediction_account_records

    path = tmp_path / "stream.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker(value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    state_path = console_state_db_path(path)

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    async def observe_updates():
        stream = _runtime_change_stream(ConnectedRequest())
        ready = await anext(stream)
        replacement = tmp_path / "replacement.db"
        with sqlite3.connect(replacement) as writer:
            writer.execute("CREATE TABLE marker(value TEXT)")
            writer.execute("INSERT INTO marker VALUES ('new snapshot')")
        os.replace(replacement, path)
        replaced = await anext(stream)
        persist_prediction_account_records(state_path, [{"venue": "kalshi", "fills": [{
            "fill_id": "stream-fill", "created_time": "2026-09-30T12:00:00Z",
        }]}], observed_at="2026-09-30T12:00:01Z")
        account_changed = await anext(stream)
        await stream.aclose()
        return ready, replaced, account_changed

    ready, replaced, account_changed = asyncio.run(observe_updates())
    assert ready == "event: ready\ndata: {}\n\n"
    assert replaced == "event: change\ndata: {}\n\n"
    assert account_changed == "event: change\ndata: {}\n\n"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "new snapshot"
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            "SELECT external_id FROM prediction_account_records WHERE record_type='fill'"
        ).fetchone()[0] == "stream-fill"


def test_console_auth_covers_html_static_and_api(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_CONSOLE_USERNAME", "operator")
    monkeypatch.setenv("NOEMA_CONSOLE_PASSWORD", "local-only-test-value")
    client = TestClient(app)
    for path in ("/", "/static/operations.mjs", "/api/operations"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")
    authorized = client.get("/api/operations", auth=("operator", "local-only-test-value"))
    assert authorized.status_code == 200


def test_deployed_console_fails_closed_without_owner_credentials(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_CONSOLE_AUTH_REQUIRED", "1")
    monkeypatch.delenv("NOEMA_CONSOLE_USERNAME", raising=False)
    monkeypatch.delenv("NOEMA_CONSOLE_PASSWORD", raising=False)
    assert TestClient(app).get("/").status_code == 503


def test_worker_snapshot_is_authenticated_verified_and_persisted(monkeypatch, tmp_path) -> None:
    source = tmp_path / "worker-source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE worker_observation (market_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO worker_observation VALUES ('real-market-record')")
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "console-replica.db"))
    monkeypatch.setenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "snapshot-test-token")
    body = gzip.compress(source.read_bytes())
    client = TestClient(app)
    denied = client.post("/internal/snapshot", content=body, headers={"Authorization": "Bearer wrong"})
    assert denied.status_code == 401
    accepted = client.post(
        "/internal/snapshot", content=body,
        headers={"Authorization": "Bearer snapshot-test-token", "Content-Type": "application/gzip"},
    )
    assert accepted.status_code == 200
    replica = tmp_path / "console-replica.db"
    assert stat.S_IMODE(replica.stat().st_mode) == 0o444
    with sqlite3.connect(replica) as conn:
        assert conn.execute("SELECT market_id FROM worker_observation").fetchone()[0] == "real-market-record"


def test_snapshot_replacement_cannot_lose_concurrent_console_account_write(
    monkeypatch, tmp_path,
) -> None:
    from noema.console_state import console_state_db_path
    from noema.dashboard_app import _merge_account_history_into_console_state
    from noema.prediction_account_history import persist_prediction_account_records

    replica = tmp_path / "console-replica.db"
    old_console_record = [{"venue": "kalshi", "fills": [{
        "fill_id": "shared-fill", "ticker": "KX-OLD", "created_time": "2026-09-30T12:00:00Z",
    }], "sync_state": {"activity": {
        "success": True, "complete": True, "high_water_at": "2026-09-30T12:00:00Z",
        "high_water_id": "cursor-old",
    }}}]
    persist_prediction_account_records(str(replica), old_console_record,
                                       observed_at="2026-09-30T12:00:01Z")
    source_records = [{"venue": "kalshi", "fills": [
        {"fill_id": "shared-fill", "ticker": "KX-WORKER-NEW", "created_time": "2026-09-30T12:01:00Z"},
        {"fill_id": "worker-fill", "ticker": "KXWORKER", "created_time": "2026-09-30T12:01:00Z"},
        {"fill_id": "state-newer-fill", "ticker": "KX-SNAPSHOT-OLD", "created_time": "2026-09-30T12:02:00Z"},
    ], "sync_state": {"activity": {
        "success": True, "complete": True, "high_water_at": "2026-09-30T12:01:00Z",
        "high_water_id": "cursor-worker",
    }}}]
    source = tmp_path / "worker-source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE worker_observation (market_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO worker_observation VALUES ('fresh-worker-record')")
        conn.execute("""CREATE TABLE canonical_pair_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL, canonical_proposition_id TEXT NOT NULL,
            semantic_status TEXT NOT NULL, settlement_equivalence TEXT NOT NULL,
            observation_hash TEXT NOT NULL UNIQUE, observation_json TEXT NOT NULL)""")
        conn.execute("INSERT INTO canonical_pair_observations VALUES (1,?,?,?,?,?,?,?)", (
            "2026-09-30T12:01:00Z", "event-1", "proposition-1", "confirmed", "unverified",
            "pair-hash-1", '{"experiment_evaluation":{"verdict":"PASS"}}',
        ))
        conn.execute("""CREATE TABLE autonomous_research_runs (
            id INTEGER PRIMARY KEY, trial_id TEXT NOT NULL, specialist TEXT NOT NULL,
            kind TEXT NOT NULL, evidence_hash TEXT NOT NULL, worker_version TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
            deadline_at TEXT NOT NULL, elapsed_seconds REAL, compute_cost_usd TEXT,
            result_json TEXT, evidence_path TEXT, mission_id TEXT,
            UNIQUE(trial_id,evidence_hash,worker_version))""")
        conn.execute("INSERT INTO autonomous_research_runs VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "trial-1", "specialist-1", "cross_venue_paper_experiment", "pair-hash-1",
            "version-1", "completed", "2026-09-30T12:01:00Z", "2026-09-30T12:01:01Z",
            "2026-09-30T12:02:00Z", None, None, '{"critic_review":{"verdict":"PASS"}}',
            None, None,
        ))
        conn.execute("""CREATE TABLE economic_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            event_type TEXT NOT NULL, amount_usd TEXT, payload_json TEXT NOT NULL)""")
        conn.execute(
            "INSERT INTO economic_events VALUES (1,?,?,?,?)",
            ("2026-09-30T12:01:01Z", "paper_cross_venue_settlement", None,
             '{"evidence_hash":"pair-hash-1","status":"settled"}'),
        )
    persist_prediction_account_records(str(source), source_records,
                                       observed_at="2026-09-30T12:01:01Z")
    monkeypatch.setenv("NOEMA_DB_PATH", str(replica))
    monkeypatch.setenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "snapshot-test-token")
    state_path = console_state_db_path(replica)
    _merge_account_history_into_console_state(str(replica), state_path)
    persist_prediction_account_records(state_path, [{"venue": "kalshi", "fills": [{
        "fill_id": "state-newer-fill", "ticker": "KX-CONSOLE-NEWER",
        "created_time": "2026-09-30T12:03:00Z",
    }], "sync_state": {"activity": {
        "success": True, "complete": True, "high_water_at": "2026-09-30T12:03:00Z",
        "high_water_id": "cursor-console-newer",
    }}}], observed_at="2026-09-30T12:03:01Z")
    real_replace = os.replace
    interleaved: list[str] = []

    def write_during_atomic_replacement(src, dst):
        if Path(dst) == replica.resolve():
            persist_prediction_account_records(state_path, [{"venue": "kalshi", "fills": [{
                "fill_id": "concurrent-fill", "ticker": "KXCONCURRENT",
                "created_time": "2026-09-30T12:02:00Z",
            }]}], observed_at="2026-09-30T12:02:01Z")
            interleaved.append("persisted-before-replace")
        return real_replace(src, dst)

    monkeypatch.setattr("noema.dashboard_app.os.replace", write_during_atomic_replacement)
    response = TestClient(app).post(
        "/internal/snapshot", content=gzip.compress(source.read_bytes()),
        headers={"Authorization": "Bearer snapshot-test-token"},
    )
    assert response.status_code == 200
    assert interleaved == ["persisted-before-replace"]
    with sqlite3.connect(replica) as conn:
        assert conn.execute("SELECT market_id FROM worker_observation").fetchone()[0] == "fresh-worker-record"
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with sqlite3.connect(state_path) as conn:
        stored = {row[0] for row in conn.execute(
            "SELECT external_id FROM prediction_account_records WHERE venue='kalshi' AND record_type='fill'"
        )}
        assert stored == {"shared-fill", "worker-fill", "state-newer-fill", "concurrent-fill"}
        shared_payload = conn.execute(
            "SELECT payload_json FROM prediction_account_records WHERE external_id='shared-fill'"
        ).fetchone()[0]
        newer_payload = conn.execute(
            "SELECT payload_json FROM prediction_account_records WHERE external_id='state-newer-fill'"
        ).fetchone()[0]
        assert 'KX-WORKER-NEW' in shared_payload
        assert 'KX-CONSOLE-NEWER' in newer_payload
        assert conn.execute(
            "SELECT high_water_at,high_water_id FROM prediction_account_sync_state "
            "WHERE venue='kalshi' AND stream='activity'"
        ).fetchone() == ("2026-09-30T12:03:00Z", "cursor-console-newer")
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT observation_hash FROM canonical_pair_observations"
        ).fetchone()[0] == "pair-hash-1"
        assert conn.execute(
            "SELECT evidence_hash FROM autonomous_research_runs"
        ).fetchone()[0] == "pair-hash-1"
        assert conn.execute(
            "SELECT count(*) FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ).fetchone()[0] == 1
    _merge_account_history_into_console_state(str(source), state_path)
    with sqlite3.connect(state_path) as conn:
        assert conn.execute("SELECT count(*) FROM canonical_pair_observations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM autonomous_research_runs").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM economic_events WHERE event_type='paper_cross_venue_settlement'"
        ).fetchone()[0] == 1
    # Repeated polling of the same provider event remains idempotent.
    persist_prediction_account_records(state_path, [{"venue": "kalshi", "fills": [{
        "fill_id": "concurrent-fill", "ticker": "KXCONCURRENT",
        "created_time": "2026-09-30T12:02:00Z",
    }]}], observed_at="2026-09-30T12:03:00Z")
    with sqlite3.connect(state_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM prediction_account_records WHERE external_id='concurrent-fill'"
        ).fetchone()[0] == 1


def test_console_research_migration_updates_a_previously_incomplete_run(tmp_path):
    import sqlite3

    from noema.dashboard_app import _merge_account_history_into_console_state

    source = tmp_path / "worker.db"
    state = tmp_path / "console-state.db"
    schema = """CREATE TABLE autonomous_research_runs(
        id INTEGER PRIMARY KEY,trial_id TEXT NOT NULL,specialist TEXT NOT NULL,kind TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,worker_version TEXT NOT NULL,status TEXT NOT NULL,
        created_at TEXT NOT NULL,completed_at TEXT,deadline_at TEXT NOT NULL,
        elapsed_seconds REAL,compute_cost_usd TEXT,result_json TEXT,evidence_path TEXT,mission_id TEXT,
        UNIQUE(trial_id,evidence_hash,worker_version))"""
    values = ("trial-a", "specialist-a", "validation", "evidence-a", "v1", "completed",
              "2026-09-30T12:00:00Z", "2026-09-30T12:05:00Z", "2026-09-30T12:10:00Z",
              5.0, "0.10", '{"observations":3}', None, None)
    with sqlite3.connect(source) as conn:
        conn.execute(schema)
        conn.execute(
            "INSERT INTO autonomous_research_runs VALUES(1," + ",".join("?" for _ in values) + ")",
            values,
        )
    with sqlite3.connect(state) as conn:
        conn.execute(schema)
        conn.execute(
            "INSERT INTO autonomous_research_runs VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values[:5], "running", values[6], None, *values[8:]),
        )

    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(state) as conn:
        row = conn.execute(
            "SELECT status,completed_at,result_json FROM autonomous_research_runs"
        ).fetchone()
    assert row == ("completed", "2026-09-30T12:05:00Z", '{"observations":3}')


def test_research_trial_migration_tracks_recency_without_replacing_identity(tmp_path):
    import sqlite3

    from noema.dashboard_app import _merge_account_history_into_console_state

    source = tmp_path / "worker.db"
    state = tmp_path / "console-state.db"
    schema = """CREATE TABLE research_trials(
        trial_id TEXT PRIMARY KEY,family TEXT NOT NULL,hypothesis TEXT NOT NULL,
        params_json TEXT NOT NULL,feature_set_version TEXT NOT NULL,status TEXT NOT NULL,
        created_at TEXT NOT NULL,parent_trial_id TEXT,status_updated_at TEXT)"""
    with sqlite3.connect(source) as conn:
        conn.execute(schema)
        conn.execute("INSERT INTO research_trials VALUES(?,?,?,?,?,?,?,?,?)", (
            "trial-id", "worker-copy", "worker hypothesis", '{"market_id":"new"}', "v1",
            "rejected", "2026-09-30T12:00:00Z", None, "2026-09-30T12:05:00Z",
        ))
    with sqlite3.connect(state) as conn:
        conn.execute(schema)
        conn.execute("INSERT INTO research_trials VALUES(?,?,?,?,?,?,?,?,?)", (
            "trial-id", "sidecar-canonical", "canonical hypothesis", '{"market_id":"old"}', "v1",
            "running", "2026-09-30T12:00:00Z", None, "2026-09-30T12:02:00Z",
        ))

    _merge_account_history_into_console_state(str(source), str(state))
    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(state) as conn:
        assert conn.execute(
            "SELECT status,status_updated_at,family,hypothesis,params_json FROM research_trials"
        ).fetchone() == (
            "rejected", "2026-09-30T12:05:00Z", "sidecar-canonical", "canonical hypothesis",
            '{"market_id":"old"}',
        )
    with sqlite3.connect(source) as conn:
        conn.execute(
            "UPDATE research_trials SET status='promoted',status_updated_at='2026-09-30T12:08:00Z' "
            "WHERE trial_id='trial-id'"
        )
    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(state) as conn:
        assert conn.execute(
            "SELECT status,status_updated_at FROM research_trials WHERE trial_id='trial-id'"
        ).fetchone() == ("promoted", "2026-09-30T12:08:00Z")
    with sqlite3.connect(source) as conn:
        conn.execute(
            "UPDATE research_trials SET status='retired',status_updated_at='2026-09-30T12:07:00Z' "
            "WHERE trial_id='trial-id'"
        )
    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(state) as conn:
        assert conn.execute(
            "SELECT status,status_updated_at FROM research_trials WHERE trial_id='trial-id'"
        ).fetchone() == ("promoted", "2026-09-30T12:08:00Z")


def test_console_research_migration_advances_mutable_trial_status(tmp_path):
    from noema.dashboard_app import _merge_account_history_into_console_state

    source, state = tmp_path / "worker.db", tmp_path / "console-state.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE research_trials(trial_id TEXT PRIMARY KEY,status TEXT NOT NULL)")
        conn.execute("INSERT INTO research_trials VALUES('trial-a','registered')")
    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE research_trials SET status='promoted' WHERE trial_id='trial-a'")
    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(state) as conn:
        assert conn.execute("SELECT status FROM research_trials WHERE trial_id='trial-a'").fetchone()[0] == "promoted"
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE research_trials SET status='retired' WHERE trial_id='trial-a'")
    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(state) as conn:
        assert conn.execute("SELECT status FROM research_trials WHERE trial_id='trial-a'").fetchone()[0] == "retired"
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE research_trials SET status='promoted' WHERE trial_id='trial-a'")
    _merge_account_history_into_console_state(str(source), str(state))
    with sqlite3.connect(state) as conn:
        assert conn.execute("SELECT status FROM research_trials WHERE trial_id='trial-a'").fetchone()[0] == "retired"


def test_worker_snapshot_rejects_corrupt_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "console-replica.db"))
    monkeypatch.setenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "snapshot-test-token")
    response = TestClient(app).post(
        "/internal/snapshot", content=gzip.compress(b"not a sqlite database"),
        headers={"Authorization": "Bearer snapshot-test-token"},
    )
    assert response.status_code == 400
    assert not (tmp_path / "console-replica.db").exists()


def test_snapshot_persistence_failure_logs_only_safe_stage_and_type(
    monkeypatch, tmp_path, caplog,
) -> None:
    source = tmp_path / "worker-source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE prediction_account_records (venue TEXT)")
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "console-replica.db"))
    monkeypatch.setenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "snapshot-test-token")

    def fail_merge(_source_path, _state_path):
        raise sqlite3.OperationalError("sensitive-row-placeholder")

    monkeypatch.setattr("noema.dashboard_app._merge_account_history_into_console_state", fail_merge)
    response = TestClient(app).post(
        "/internal/snapshot", content=gzip.compress(source.read_bytes()),
        headers={"Authorization": "Bearer snapshot-test-token"},
    )

    assert response.status_code == 500
    assert "stage=merge_console_state exception_type=OperationalError" in caplog.text
    assert "sensitive-row-placeholder" not in caplog.text


def test_replication_backup_contains_committed_worker_records(tmp_path) -> None:
    from noema.console_replication import _snapshot_image

    source = tmp_path / "worker.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE observations (evidence TEXT)")
        conn.execute("INSERT INTO observations VALUES ('official-venue-observation')")
    image = gzip.decompress(_snapshot_image(str(source)))
    replica = tmp_path / "backup.db"
    replica.write_bytes(image)
    with sqlite3.connect(replica) as conn:
        assert conn.execute("SELECT evidence FROM observations").fetchone()[0] == "official-venue-observation"
    assert source.exists()


def test_worker_metadata_is_persisted_without_credentials(monkeypatch, tmp_path) -> None:
    import base64
    import json

    from noema.console_replication import load_worker_metadata, persist_worker_metadata

    metadata = {
        "worker_commit": "a" * 40,
        "cognition": {
            "provider": "openai",
            "deployment": "gpt-model",
            "enabled": True,
            "configured": True,
        },
        "api_key": "must-not-be-preserved",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(metadata).encode()).decode()
    from noema.console_replication import decode_worker_metadata

    safe_metadata = decode_worker_metadata(encoded)
    persist_worker_metadata(str(tmp_path / "replica.db"), safe_metadata)
    loaded = load_worker_metadata(str(tmp_path / "replica.db"))
    assert loaded == safe_metadata
    assert "api_key" not in json.dumps(loaded)


def test_worker_cognition_diagnostics_use_snapshot_metadata(monkeypatch, tmp_path) -> None:
    from noema.console_replication import persist_worker_metadata

    path = tmp_path / "cognition-replica.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker(value TEXT)")
    persist_worker_metadata(str(path), {
        "worker_commit": "b" * 40,
        "cognition": {
            "provider": "openai", "deployment": "gpt-model",
            "enabled": True, "configured": True,
        },
    })
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    monkeypatch.setenv("NOEMA_RUNTIME_SOURCE", "worker-sqlite-replica")
    health = TestClient(app).get("/api/provider-health").json()
    assert health["configured_provider"] == "openai"
    assert health["configured_provider_ready"] is True
    assert health["connectivity"] == "not probed by console"
    assert health["worker_commit"] == "b" * 40
    cognition = TestClient(app).get("/api/cognition").json()
    assert cognition["provider"] == "openai"
    assert cognition["deployment"] == "gpt-model"
    assert cognition["configured"] is True
    assert cognition["worker_commit"] == "b" * 40
