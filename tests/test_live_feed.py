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


def test_cached_error_retries_soon_and_keeps_last_good(monkeypatch):
    monkeypatch.setattr(live_feed, "_CACHE", {})
    assert live_feed._cached("k", 300, lambda: {"as_of": "x", "v": 1})["v"] == 1
    live_feed._CACHE["k"] = (-1e9, live_feed._CACHE["k"][1])  # expire

    def boom():
        raise RuntimeError("database is locked")
    v = live_feed._cached("k", 300, boom)
    assert v["v"] == 1 and "locked" in v["last_error"]
    assert live_feed._CACHE["k"][0] < live_feed.time.monotonic() - 290


def test_treasury_failing_source_goes_red_with_last_good(tmp_path):
    import json
    import time as _t
    from datetime import UTC, datetime, timedelta
    db = tmp_path / "noema.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE equity_feed_readings (at TEXT, status TEXT, total_usd TEXT, deposit_usd TEXT, sources_json TEXT)")
    now = datetime.now(UTC)
    old = (now - timedelta(seconds=600)).isoformat()
    c.execute("INSERT INTO equity_feed_readings VALUES (?,?,?,?,?)", (old, "complete", "5", "0", json.dumps(
        {"solana": {"status": "ok", "equity_usd": "4", "read_at": old}, "kalshi": {"status": "ok", "equity_usd": "1", "read_at": old}})))
    c.execute("INSERT INTO equity_feed_readings VALUES (?,?,?,?,?)", (now.isoformat(), "stale", None, "0", json.dumps(
        {"solana": {"status": "error", "error": "Timeout", "read_at": now.isoformat()},
         "kalshi": {"status": "ok", "equity_usd": "1", "read_at": now.isoformat()}})))
    c.commit()
    c.close()
    v = {x["venue"]: x for x in live_feed.treasury_section(str(db), _t.time())["venues"]}
    assert v["kalshi"]["fresh"] == "ok" and v["kalshi"]["age_s"] < 5
    assert v["solana"]["fresh"] == "red" and v["solana"]["usd"] == 4 and v["solana"]["age_s"] > 590
