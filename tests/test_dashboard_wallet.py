from fastapi.testclient import TestClient

from noema.dashboard_app import app


def test_wallet_policy_endpoint_exposes_safe_public_state() -> None:
    client = TestClient(app)
    response = client.get("/api/wallet-policy")
    assert response.status_code == 200
    payload = response.json()
    assert payload["master_halt"] is True
    assert payload["signer_state"] == "disabled"
    assert "private_key" not in payload
