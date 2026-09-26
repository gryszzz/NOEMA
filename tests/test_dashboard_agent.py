from fastapi.testclient import TestClient

from noema.dashboard_app import app


def test_agent_endpoint_is_safe_without_running_agent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "noema.db"))
    client = TestClient(app)
    response = client.get("/api/agent")
    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_id"] == "noema"
    assert payload["alive"] is False
