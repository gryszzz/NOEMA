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
