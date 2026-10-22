import sqlite3

from noema import live_feed


def test_build_live_empty_db_is_safe(tmp_path, monkeypatch):
    db = tmp_path / "noema.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(live_feed, "_CACHE", {}, raising=False)
    d = live_feed.build_live(str(db))
    assert d["paper_only"] is True
    for k in ("launch", "candles", "smart", "agents", "looking", "treasury", "pm_history", "near_close", "xv"):
        assert k in d and "as_of" in d[k]


def test_pumpportal_note_detects_unfunded_key(tmp_path, monkeypatch):
    log = tmp_path / "ls.log"
    log.write_text("x pumpportal msg 'subscribeTokenTrade' ... funded with at least 0.02 SOL.\n")
    monkeypatch.setenv("NOEMA_LAUNCH_LOG", str(log))
    assert "not funded" in live_feed._pumpportal_note()


def test_live_endpoint(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from noema import dashboard_app
    db = tmp_path / "noema.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(dashboard_app, "_db_path", lambda: str(db))
    monkeypatch.setattr(live_feed, "_CACHE", {}, raising=False)
    r = TestClient(dashboard_app.app).get("/api/live")
    assert r.status_code == 200 and r.json()["schema"] == "tev.live.v1"
