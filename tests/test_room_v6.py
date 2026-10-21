import json
import sqlite3

from fastapi.testclient import TestClient

from noema import room_v6
from noema.room_v6 import build_room_v6


def _db(tmp_path):
    p = tmp_path / "n.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE forecast_ledger (id INTEGER PRIMARY KEY, created_at TEXT, venue TEXT, "
              "market_id TEXT, snapshot_json TEXT, forecast_json TEXT, opportunity_json TEXT, action_json TEXT)")
    c.execute("INSERT INTO forecast_ledger (created_at, venue, market_id, snapshot_json) VALUES "
              "('2026-01-01 00:00:00','kalshi:production','K1',?)", (json.dumps({"title": "T"}),))
    c.execute("CREATE TABLE trench_provider_health (provider TEXT, state TEXT, last_attempt_at TEXT, "
              "last_success_at TEXT, last_failure_at TEXT, consecutive_failures INT, next_retry_at TEXT, "
              "last_error_class TEXT, updated_at TEXT)")
    c.execute("INSERT INTO trench_provider_health VALUES ('gt','healthy',NULL,NULL,NULL,0,NULL,NULL,NULL)")
    c.commit()
    c.close()
    return str(p)


def test_room_v6_read_only_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("NOEMA_SOLANA_SIGNER_ENABLED", raising=False)
    out = build_room_v6(_db(tmp_path), fetch=False)
    assert out["paper"] is True
    lanes = {l["lane"]: l for l in out["lanes"]}
    assert lanes["kalshi"]["recent"][0]["title"] == "T"
    assert lanes["polymarket"]["status"] == "no data"
    assert lanes["solana_dex"]["status"] == "no data"
    assert out["providers"][0]["provider"] == "gt"
    assert {s["family"]: s["state"] for s in out["signers"]}["solana"] == "OFF"
    assert out["accuracy_leaderboard"] is None
    blob = json.dumps(out).lower()
    for bad in ("secret", "private", "seed"):
        assert bad not in blob


def test_room_v6_missing_db_and_leaderboard_passthrough(tmp_path):
    rows = [{"agent": "a", "brier": 0.1, "weight": 1.0, "benched": False}]
    out = build_room_v6(str(tmp_path / "none.db"), fetch=False,
                        command_center={"accuracy_leaderboard": rows})
    assert out["lanes"] == [] and out["accuracy_leaderboard"] == rows
    report = {"agents": [{"agent": "tev_judge", "agent_id": "tev_judge", "brier": 0.2,
                          "calibration_error": 0.04, "weight": 0.1, "benched": False}]}
    unwrapped = build_room_v6(str(tmp_path / "none.db"), fetch=False,
                              command_center={"accuracy": report})
    assert unwrapped["accuracy_leaderboard"] == report["agents"]


def test_signer_armed_only_when_flag_is_one(monkeypatch):
    monkeypatch.setenv("NOEMA_EVM_SIGNER_ENABLED", "1")
    assert {s["family"]: s["state"] for s in room_v6.signer_status()}["evm"] == "ARMED"


def test_accuracy_leaderboard_uses_private_api_only():
    from pathlib import Path
    page = Path("noema/static/trading-room-v6.html").read_text(encoding="utf-8")
    assert "/api/accuracy" in page and "accuracy_leaderboard" in page
    public = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("site").rglob("*")
        if path.is_file() and path.suffix in {".html", ".mjs", ".js", ".md"}
    )
    assert "/api/accuracy" not in public


def test_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setattr(room_v6, "evm_balances", lambda fetch=True: [])
    from noema.dashboard_app import app
    c = TestClient(app)
    assert "CRAWLER BAY" in c.get("/trading-room").text
    assert c.get("/trading-room-v5").status_code == 200
    assert c.get("/api/room-v6").status_code == 200
