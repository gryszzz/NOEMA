from fastapi.testclient import TestClient

from noema.dashboard_app import app


def test_doctor_and_cognition_endpoints_do_not_expose_keys(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("NOEMA_FOUNDRY_API_KEY", "hidden-secret")
    monkeypatch.setenv(
        "NOEMA_FOUNDRY_ENDPOINT",
        "https://resource.openai.azure.com",
    )
    monkeypatch.setenv("NOEMA_FOUNDRY_DEPLOYMENT", "astra")
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "noema.db"))

    client = TestClient(app)
    doctor = client.get("/api/doctor")
    cognition = client.get("/api/cognition")

    assert doctor.status_code == 200
    assert cognition.status_code == 200
    assert "hidden-secret" not in doctor.text
    assert "hidden-secret" not in cognition.text
    assert cognition.json()["deployment"] == "astra"
