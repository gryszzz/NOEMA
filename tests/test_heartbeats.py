import json
import os
import sqlite3
import time

from noema import heartbeats as hb


def _db(tmp_path):
    p = tmp_path / "noema.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE equity_feed_readings (at TEXT, sources_json TEXT)")
    now = time.time()
    iso = hb._iso
    c.execute("INSERT INTO equity_feed_readings VALUES (?,?)", (iso(now - 400), json.dumps(
        {"kalshi": {"status": "ok", "read_at": iso(now - 400), "equity_usd": 14.9}})))
    c.execute("INSERT INTO equity_feed_readings VALUES (?,?)", (iso(now - 5), json.dumps(
        {"solana": {"status": "ok", "read_at": iso(now - 5), "equity_usd": 26.8},
         "kalshi": {"status": "error", "read_at": iso(now - 5), "error": "GET https://x/y?api_key=SECRET123 401"}})))
    c.execute("CREATE TABLE launch_stream (received_at TEXT)")
    c.execute("INSERT INTO launch_stream VALUES (?)", (iso(now - 3),))
    c.commit()
    c.close()
    return str(p)


def test_status_thresholds():
    assert hb.status_for(10, 60) == "fresh"
    assert hb.status_for(121, 60) == "amber"
    assert hb.status_for(361, 60) == "red"
    assert hb.status_for(None, 60) == "red"
    assert hb.status_for(10, 60, failed_since_success=True) == "amber"


def test_sanitize_strips_secrets():
    s = hb.sanitize("GET https://api.x.com/v1?api-key=abc123 Bearer abcdefghijkl token=zzz " + "A" * 50)
    assert "abc123" not in s and "abcdefghijkl" not in s and "zzz" not in s and "A" * 50 not in s


def test_snapshot_wallets_beats_and_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_HEARTBEAT_DIR", str(tmp_path / "hb"))
    monkeypatch.setattr(hb, "RUN_DIR", str(tmp_path / "norun"))
    db = _db(tmp_path)
    hb.beat("feed:custom", ok=False, error="boom key=SECRET", cadence_s=30)
    snap = hb.snapshot(db)
    by = {c["name"]: c for c in snap["connections"]}
    assert by["wallet:solana"]["status"] == "fresh"
    k = by["wallet:kalshi"]
    assert k["status"] == "red" and "SECRET123" not in (k["last_error"] or "")
    assert by["feed:launch_stream"]["status"] == "fresh"
    assert by["feed:custom"]["status"] == "red" and "SECRET" not in by["feed:custom"]["last_error"]
    assert "SECRET" not in json.dumps(snap)
    out = tmp_path / "heartbeats.json"
    hb.write_json(snap, str(out))
    loaded = hb.load(str(out))
    assert hb.stale_sources(["wallet:kalshi", "feed:launch_stream"], loaded) == ["wallet:kalshi"]
    assert hb.stale_sources(["wallet:"], loaded) == ["wallet:kalshi"]
    monkeypatch.setenv("NOEMA_HEARTBEATS_JSON", str(out))
    assert hb.gate("trench-desks", ["wallet:kalshi"]) == ["wallet:kalshi"]
    sk = hb.recent_skips()
    assert sk and sk[0]["reason"] == "skip: stale wallet:kalshi" and sk[0]["decision"] == "skip"
    assert hb.gate("trench-desks", ["feed:launch_stream"]) == []


def test_missing_registry_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_HEARTBEATS_JSON", str(tmp_path / "nope.json"))
    assert hb.stale_sources(["feed:x"]) == []
    assert os.path.exists(tmp_path) and hb.to_epoch(1792696080000) == 1792696080.0
