import json
import sqlite3

from fastapi.testclient import TestClient

from noema import dashboard_app
from noema.trading_room import build_trading_room, live_flags
from noema.trench_desks import TrenchDeskStore


def _seed(path):
    store = TrenchDeskStore(str(path))
    store.ensure_desks(2, 160.0)
    store.event(None, "desk_cycle", pools_seen=5, prefilter_passed=2, entry_passed=1,
                entries=1, picks=["M1"], rejections={"liquidity below desk floor": 3},
                feed=[{"mint": "M1", "symbol": "AAA", "stage": "desk", "liquidity_usd": 20000,
                       "reason": None}])
    store.event(None, "equity_snapshot", equity_usd=159.0, pnl_after_costs_usd=-1.0)
    store.event(None, "equity_snapshot", equity_usd=161.0, pnl_after_costs_usd=1.0)
    store.conn.commit()
    store.close()


def test_build_trading_room_aggregates(tmp_path):
    db = tmp_path / "n.db"
    _seed(db)
    room = build_trading_room(str(db), venues={"venues": [{"venue": "Kalshi", "account": {
        "status": "authenticated_read_only", "cash_balance_usd": "1.5", "positions": []}}]},
        wallets={"networks": [{"chain": "solana", "address": "J57o", "sol": "0.1"}]})
    assert len(room["seats"]) == 2 and all(s["hired"] for s in room["seats"])
    assert room["chain"]["crawler"] == 5 and room["chain"]["desk"] == 1
    assert room["feed"][0]["symbol"] == "AAA"
    assert [p["pnl_usd"] for p in room["equity_series"]] == [-1.0, 1.0]
    assert {v["venue"] for v in room["venues"]} == {"Kalshi", "Solana wallet"}
    assert all(v["execution"] == "disabled" for v in room["venues"])
    assert room["autonomy"]["level"] == "SHADOW"


def test_live_flags_default_off(monkeypatch):
    for k in ("NOEMA_EXECUTION_GATEWAY_ENABLED", "NOEMA_ALLOW_LIVE_ORDERS"):
        monkeypatch.setenv(k, "0")
    flags = live_flags()
    assert flags["any_live"] is False


def test_trading_room_endpoints(tmp_path, monkeypatch):
    db = tmp_path / "n.db"
    _seed(db)
    monkeypatch.setattr(dashboard_app, "_db_path", lambda: str(db))

    async def none(*a, **k):
        return None
    monkeypatch.setattr(dashboard_app, "build_prediction_venue_status", none)
    monkeypatch.setattr(dashboard_app, "wallet_status", none)
    client = TestClient(dashboard_app.app)
    body = client.get("/api/trading-room").json()
    assert body["paper"] is True and len(body["seats"]) == 2
    text = json.dumps(body).lower()
    assert "private_key" not in text and "secret" not in text
    page = client.get("/trading-room")
    assert page.status_code == 200 and "/api/trading-room" in page.text
    assert "/trading-room" in client.get("/").text


def test_feed_history_is_real_cycle_data(tmp_path):
    from noema.trading_room import build_trading_room
    room = build_trading_room(str(tmp_path / "missing.db"))
    assert room["feed_history"] == []
    assert room["interval_seconds"] > 0


def test_command_center_empty_db_is_explicit(tmp_path):
    from noema.command_center import build_command_center
    d = build_command_center(str(tmp_path / "none.db"), fetch_tokens=False)
    assert d["missing_db"] is True and d["wallet"]["tokens"] == []


def test_command_center_real_tables(tmp_path):
    from noema.command_center import build_command_center
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE forecast_ledger (id INTEGER PRIMARY KEY, created_at, venue, market_id, snapshot_json, forecast_json, opportunity_json, action_json)")
    c.execute("CREATE TABLE outcomes (venue, market_id, outcome_yes, resolved_at)")
    c.execute("INSERT INTO forecast_ledger VALUES (1,'2026-01-01','k','m','{}','{\"model_version\":\"x\",\"probability_yes\":0.8}','{\"forecast\":{\"probability_yes\":0.8,\"model_version\":\"x\"},\"robust_edge\":0.1}','{}')")
    c.execute("INSERT INTO outcomes VALUES ('k','m',1,'2026-01-02')")
    c.commit(); c.close()
    d = build_command_center(str(db), fetch_tokens=False)
    assert d["ledger"]["brier"]["x"]["brier"] == 0.04
    assert d["markets"]["independent"][0]["edge"] == 0.1
    assert d["autonomy"]["resolved"] == 1
    assert "not wired" in d["coordinator"]["status"] or d["coordinator"]["coordinator_module_present"]


def test_room_v4_offline(tmp_path):
    from noema.room_v4 import build_room_v4
    d = build_room_v4(str(tmp_path / "none.db"), fetch=False)
    assert d["charts"] == [] and d["social"]["status"] == "unavailable"


def test_paper_equity_series_is_lineage_safe(tmp_path):
    import json
    import sqlite3

    from noema.command_center import paper_equity_series
    db = tmp_path / "e.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE trench_desks (desk_id TEXT, starting_bankroll_usd REAL, parent_desk_id TEXT)")
    c.execute("CREATE TABLE trench_desk_events (id INTEGER PRIMARY KEY, event TEXT, payload_json TEXT)")
    c.executemany("INSERT INTO trench_desks VALUES (?,?,?)",
                  [("a-g1", 80, None), ("b-g1", 80, None), ("a-g2", 75, "a-g1")])
    # old double-counted snapshot: 80+80+75 style equity, true P&L -5
    c.execute("INSERT INTO trench_desk_events (event, payload_json) VALUES ('equity_snapshot', ?)",
              (json.dumps({"equity_usd": 230.0, "pnl_after_costs_usd": -5.0}),))
    c.commit()
    assert paper_equity_series(c) == [155.0]


def test_stale_mark_reports_no_unrealized():
    from noema.trading_room import _mark_age_seconds
    assert _mark_age_seconds(None) is None
    assert _mark_age_seconds("2020-01-01T00:00:00+00:00") > 300
