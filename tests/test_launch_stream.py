import sqlite3

from noema import launch_stream as ls
from noema import meta_launch as ml
from noema import pmus_history as ph


def test_pumpportal_create_and_trade(tmp_path):
    c = ls.connect(str(tmp_path / "s.db"))
    f: dict = {}
    assert ls.handle_pumpportal(c, {"txType": "create", "mint": "M1", "signature": "S1", "name": "Dog Moon",
                                    "symbol": "DMOON", "solAmount": 1.0}, f) == "M1"
    ls.handle_pumpportal(c, {"txType": "buy", "mint": "M1", "signature": "S2", "solAmount": 0.5}, f)
    assert c.execute("SELECT COUNT(*) FROM launch_trades").fetchone()[0] == 1
    c.commit()
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE trench_launches (mint TEXT, first_pool_at TEXT, discovery_json TEXT)")
    got = ml.load_launches(c, "2000-01-01")
    assert got and got[0]["d"]["stream_only"]
    ok, bad, _ = ml.shield(got[0], {}, {})
    assert not ok and "stream-only" in bad[0]


def test_rpc_url_never_needs_key(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert "helius" not in ls.rpc_ws_url()
    monkeypatch.setenv("HELIUS_API_KEY", "x")
    assert ls.rpc_ws_url().startswith("wss://mainnet.helius-rpc.com/")


def test_pmus_yes_price():
    assert ph.yes_price({"marketSides": [{"long": True, "price": "0.638"}]}) == 0.638
