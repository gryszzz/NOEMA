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


def _ev(name, body):
    import base64
    import hashlib
    return "Program data: " + base64.b64encode(hashlib.sha256(f"event:{name}".encode()).digest()[:8] + body).decode()


def test_rpc_trade_decode_follow_window(tmp_path):
    c = ls.connect(str(tmp_path / "t.db"))
    mint, user = bytes(range(1, 33)), bytes([9]) * 32
    s = lambda x: len(x).to_bytes(4, "little") + x
    create = _ev("CreateEvent", s(b"Dog") + s(b"DOG") + s(b"u") + mint + bytes(32) + user)
    trade = _ev("TradeEvent", mint + (10**9).to_bytes(8, "little") + (5 * 10**6).to_bytes(8, "little") + b"\x01" + user
                + (0).to_bytes(8, "little") + (30 * 10**9).to_bytes(8, "little") + (10**15).to_bytes(8, "little") + bytes(40))
    f: dict = {}
    assert ls.handle_rpc_logs(c, "S0", [trade], f, now=100) == 0  # not followed yet
    assert ls.handle_rpc_logs(c, "S1", ["Program log: x", create, trade, trade], f, now=100) == 2
    r = c.execute("SELECT mint, side, sol, tokens, trader, market_cap_sol FROM launch_trades WHERE signature='S1'").fetchone()
    assert r == (ls.b58(mint), "buy", 1.0, 5.0, ls.b58(user), 30.0)
    assert ls.handle_rpc_logs(c, "S2", [trade], f, now=100 + ls.TRADE_FOLLOW_S + 1) == 0  # window over
    ls.prune_follow(f, now=100 + ls.TRADE_FOLLOW_S + 1)
    assert not f


def test_b58_known():
    assert ls.b58(bytes(32)) == "1" * 32
    assert ls.b58(bytes([0, 0, 1])) == "112"
