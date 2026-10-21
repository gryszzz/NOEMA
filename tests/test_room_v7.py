import json
import sqlite3

from fastapi.testclient import TestClient

from noema import room_v7
from noema.room_v7 import build_room_v7, stripe_panel


def _db(tmp_path, name="n.db"):
    p = tmp_path / name
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE trench_desk_events (id INTEGER PRIMARY KEY, ts TEXT, desk_id TEXT, event TEXT, "
              "payload_json TEXT)")
    cycle = {"pools_seen": 10, "prefilter_passed": 4, "holder_checked": 2, "entry_passed": 1, "entries": 0,
             "pumpfun_new_launches": 5, "rejections": {"liquidity below desk floor": 6},
             "chief": {"min_probability": 0.6, "min_confidence": 0.55},
             "feed": [{"symbol": "AAA", "stage": "rejected", "reason": "liquidity below desk floor",
                       "price_change_m5": 2.0, "candles": [[1, 1, 1, 1, 1.0, 5], [2, 1, 1, 1, 1.2, 5]]},
                      {"symbol": "BBB", "stage": "filters", "price_change_m5": 1.0, "candles": []}]}
    c.executemany("INSERT INTO trench_desk_events (ts, desk_id, event, payload_json) VALUES (?,?,?,?)", [
        ("2026-10-21T20:00:00+00:00", None, "desk_cycle", json.dumps(cycle)),
        ("2026-10-21T20:00:01+00:00", None, "chief_decision",
         json.dumps({"approved": False, "symbol": "AAA", "probability": 0.4, "confidence": 0.8, "reason": "p<0.6"})),
        ("2026-10-21T20:00:02+00:00", None, "equity_snapshot", json.dumps({"equity_usd": 100})),
    ])
    c.execute("CREATE TABLE capital_guard (id INTEGER PRIMARY KEY, peak_equity TEXT, floor TEXT, equity TEXT, "
              "day TEXT, day_start_equity TEXT, tripped INT, trip_reason TEXT, updated_at TEXT)")
    c.execute("INSERT INTO capital_guard VALUES (1,'10','7.5','9','2026-10-21','10',0,NULL,'x')")
    c.commit()
    c.close()
    return str(p)


def test_room_v7_is_built_from_real_rows(tmp_path):
    out = build_room_v7(_db(tmp_path), fetch=False)
    assert out["paper"] is True
    assert out["funnel"]["latest"]["crawled"] == 10 and out["funnel"]["latest"]["judged"] == 1
    assert out["funnel"]["rejects"] == [{"reason": "liquidity below desk floor", "count": 6}]
    assert out["funnel"]["scan"][0]["closes"] == [1.0, 1.2]
    assert [e["event"] for e in out["events"]] == ["chief_decision", "desk_cycle"]  # no equity noise
    assert out["chief"]["last"]["symbol"] == "AAA"
    assert out["equity"]["guard"]["day_pnl_usd"] == -1.0
    assert out["regime"]["n"] == 2
    assert "VETO AAA" in out["headline"]
    assert build_room_v7(_db(tmp_path, "m.db"), since_id=2, fetch=False)["events"] == []


def test_room_v7_missing_db_has_explicit_empty_state(tmp_path):
    out = build_room_v7(str(tmp_path / "none.db"), fetch=False)
    assert out["funnel"]["scan"] == [] and out["regime"]["label"] == "UNKNOWN"


def test_stripe_panel_requires_restricted_key(monkeypatch):
    assert stripe_panel({})["status"] == "not_connected"
    assert stripe_panel({"NOEMA_STRIPE_READ_KEY": "sk_live_x"})["status"] == "refused"
    room_v7._CACHE.pop("stripe", None)
    calls = []

    def fake(key, path, params=None):
        calls.append(path)
        return {"balance": {"livemode": True, "available": [{"amount": 1234, "currency": "usd"}], "pending": []},
                "charges": {"data": [{"created": 1, "amount": 500, "currency": "usd", "status": "succeeded"}]},
                "payouts": {"data": []}}[path]

    monkeypatch.setattr(room_v7, "_stripe_get", fake)
    out = stripe_panel({"NOEMA_STRIPE_READ_KEY": "rk_live_test"})
    assert out["status"] == "connected" and out["available"][0]["amount_minor"] == 1234
    assert "rk_live_test" not in json.dumps(out)
    assert calls == ["balance", "charges", "payouts"]
    room_v7._CACHE.pop("stripe", None)


def test_room_v7_route_and_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_DB_PATH", _db(tmp_path))
    monkeypatch.setattr(room_v7, "prices", dict)
    from noema.dashboard_app import app
    client = TestClient(app)
    r = client.get("/api/room-v7?since=0")
    assert r.status_code == 200 and r.json()["funnel"]["latest"]["crawled"] == 10
    assert client.get("/static/room-v7.js").status_code == 200
    assert "/static/room-v7.js" in client.get("/trading-room").text


def test_v7_grid_accounts_for_every_v6_row():
    from pathlib import Path
    css = (Path(__file__).parents[1] / "noema" / "static" / "room-v7.css").read_text()
    html = (Path(__file__).parents[1] / "noema" / "static" / "trading-room-v6.html").read_text()
    if "culture-row" in html:
        assert ".culture-row{grid-row:6!important}" in css and ".left{grid-row:4/7!important}" in css
