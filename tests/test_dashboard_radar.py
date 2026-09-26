from fastapi.testclient import TestClient

from noema.dashboard_app import app


def test_radar_endpoint_is_safe_without_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "missing.db"))
    client = TestClient(app)
    response = client.get("/api/radar")
    assert response.status_code == 200
    assert response.json() == []
