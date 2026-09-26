from fastapi.testclient import TestClient

from noema.dashboard_app import app


def test_dashboard_root_renders_console() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "NOEMA // OPS" in response.text
    assert "Paper research" in response.text
    assert client.get("/static/brand/noema-face.png").status_code == 200


def test_overview_endpoint_is_safe_without_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "missing.db"))
    client = TestClient(app)
    response = client.get("/api/overview")
    assert response.status_code == 200
    assert response.json()["database_present"] is False
