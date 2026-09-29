import asyncio
import sqlite3

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
