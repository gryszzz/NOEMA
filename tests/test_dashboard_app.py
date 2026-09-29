import asyncio
import gzip
import sqlite3
import stat

from fastapi.testclient import TestClient

from noema.dashboard_app import _runtime_change_stream, app


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
    async def fake_status():
        return {"execution_enabled": False, "venues": [], "cross_venue_comparison": {"matches": []}}

    monkeypatch.setattr("noema.dashboard_app.build_prediction_venue_status", fake_status)
    response = TestClient(app).get("/api/prediction-venues")
    assert response.status_code == 200
    assert response.json()["execution_enabled"] is False


def test_stripe_projection_endpoint_is_read_only_and_safe_without_database(monkeypatch, tmp_path) -> None:
    path = tmp_path / "missing-stripe.db"
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    response = TestClient(app).get("/api/stripe-economy")
    assert response.status_code == 200
    assert response.json()["status"] == "not_observed"
    assert not path.exists()


def test_runtime_stream_notifies_after_another_connection_commits(monkeypatch, tmp_path) -> None:
    path = tmp_path / "stream.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker(value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("NOEMA_DB_PATH", str(path))
    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    async def observe_commit():
        stream = _runtime_change_stream(ConnectedRequest())
        ready = await anext(stream)
        writer = sqlite3.connect(path)
        writer.execute("INSERT INTO marker VALUES ('commit')")
        writer.commit()
        writer.close()
        changed = await anext(stream)
        await stream.aclose()
        return ready, changed

    ready, changed = asyncio.run(observe_commit())
    assert ready == "event: ready\ndata: {}\n\n"
    assert changed == "event: change\ndata: {}\n\n"


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


def test_worker_snapshot_rejects_corrupt_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "console-replica.db"))
    monkeypatch.setenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "snapshot-test-token")
    response = TestClient(app).post(
        "/internal/snapshot", content=gzip.compress(b"not a sqlite database"),
        headers={"Authorization": "Bearer snapshot-test-token"},
    )
    assert response.status_code == 400
    assert not (tmp_path / "console-replica.db").exists()


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
