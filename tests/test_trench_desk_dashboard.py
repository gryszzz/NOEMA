from fastapi.testclient import TestClient

from noema.trench_desks import TrenchDeskStore


def test_trench_desk_endpoints(tmp_path, monkeypatch):
    db = tmp_path / "dash.db"
    store = TrenchDeskStore(str(db))
    store.ensure_desks(5, 400)
    store.close()
    monkeypatch.setenv("NOEMA_DB_PATH", str(db))
    from noema.dashboard_app import app

    client = TestClient(app)
    data = client.get("/api/trench-desks").json()
    assert data["paper"] is True and data["live_execution"] is False
    assert len(data["desks"]) == 5 and data["desks"][0]["equity_usd"] == 80.0
    page = client.get("/trench-desks")
    assert page.status_code == 200 and "PAPER ONLY" in page.text
