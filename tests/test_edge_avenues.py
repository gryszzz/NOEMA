import sqlite3

from noema import edge_avenues as ea
from noema.x_edge import precise_query


def test_mm_fills_only_on_trades_through_and_settles():
    q = [(0.0, 0.40, 0.50), (60.0, 0.40, 0.50)]
    # at our bid 0.41: a sell at 0.41 is NOT through; 0.40 is
    tr = [(10.0, 0.41, "no"), (20.0, 0.40, "no"), (30.0, 0.50, "yes")]
    r = ea.simulate_mm(q, tr, result=1, close_ts=10_000)
    assert r["fills"] == 2 and r["end_inventory"] == 0
    assert abs(r["pnl"] - (0.49 - 0.41 - ea.maker_fee(0.41) - ea.maker_fee(0.49))) < 1e-9


def test_mm_gate_blocks_narrow_spread():
    assert ea.simulate_mm([(0.0, 0.40, 0.42)], [(1.0, 0.30, "no")], 1, 10_000) is None


def test_carry_backtest_uses_only_past_rates():
    c = sqlite3.connect(":memory:")
    c.executescript(ea.SCHEMA)
    rows = [("hyperliquid", "BTC", h * 3600, 0.0 if h < 72 else 0.001) for h in range(400)]
    c.executemany("INSERT INTO funding VALUES (?,?,?,?)", rows)
    r = ea.backtest_carry(c)["horizon_720h"]["per_market"]["hyperliquid:BTC"]
    # entry only once 72 strictly-past hours average above the gate -> the first high hour is never collected
    assert r["n"] == 1 and r["mean"] < (400 - 72) * 0.001


def test_precise_query_strips_cashtags():
    assert "$" not in precise_query("($SOL OR solana) launch")
    assert precise_query("$WIF pump") == '"WIF" pump'


def test_throttle_factor(tmp_path):
    import json as _j

    from noema import agent_lifecycle as L
    db = str(tmp_path / "life.db")
    c = L.connect(db)
    assert L.throttle_factor(db, "kalshi_mm") == 1
    a = L.hire(c, {"kind": "kalshi_mm", "filters": []}, generation=0, parent=None, origin="t", kind="kalshi_mm")
    c.execute("UPDATE agents SET stats_json=? WHERE agent_id=?", (_j.dumps({"n": 40, "mean_net": -0.1}), a))
    c.commit()
    assert L.throttle_factor(db, "kalshi_mm") == 4
    c.execute("UPDATE agents SET status='fired' WHERE agent_id=?", (a,))
    c.commit()
    assert L.throttle_factor(db, "kalshi_mm") == 8
