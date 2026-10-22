import sqlite3
from datetime import UTC, datetime

from noema import edge_strategies as es
from noema import proven_sizing as ps


def _feed(path, rows):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE opp_items (id TEXT PRIMARY KEY, source TEXT, tier INTEGER, title TEXT, venue TEXT,
        ref TEXT, first_seen TEXT, last_seen TEXT, source_at TEXT, price_kind TEXT, ref_price REAL, last_price REAL,
        edge REAL, score REAL, reason TEXT, url TEXT, status TEXT, expired_at TEXT, payload_json TEXT)""")
    for i, (src, reason, kind, ref, last) in enumerate(rows):
        c.execute("INSERT INTO opp_items (id, source, reason, price_kind, ref_price, last_price, status, payload_json)"
                  " VALUES (?,?,?,?,?,?, 'expired', '{}')", (str(i), src, reason, kind, ref, last))
    c.commit()
    c.close()


def test_reason_type_strips_numbers():
    assert ps.reason_type("alpha", "liq $111,952 · x") == ps.reason_type("alpha", "liq $5,000 · y")


def test_costs_and_directionality():
    assert ps.item_return("market", 0.4, 0.5, None) is None
    assert abs(ps.item_return("market", 0.4, 0.5, "yes") - 0.07) < 1e-9
    assert abs(ps.item_return("token", 1.0, 1.1) - (0.1 - 0.012)) < 1e-9


def test_proven_needs_n_and_positive_lower_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_PROVEN_MIN_N", "30")
    good = [("alpha", f"rug screens pass · liq ${i}", "token", 1.0, 1.10 + (i % 3) * 0.01) for i in range(40)]
    few = [("trench", "pool sweep", "token", 1.0, 1.5) for _ in range(5)]
    _feed(tmp_path / "f.db", good + few)
    b = ps.bucket_scores(str(tmp_path / "f.db"))
    assert b["alpha:rug screens pass"]["proven"]
    assert not b["trench:pool sweep"]["proven"]


def _book(tmp_path):
    conn = sqlite3.connect(tmp_path / "noema.db")
    es.ensure_schema(conn)
    return conn


def test_size_for_unproven_is_probe_and_proven_gets_size(tmp_path, monkeypatch):
    monkeypatch.delenv("NOEMA_UNPROVEN_MODE", raising=False)
    conn = _book(tmp_path)
    es.allocate(conn, datetime.now(UTC))
    assert es.size_for(conn, "smart_money", {}) <= 2.5
    _feed(tmp_path / "opportunity_feed.db",
          [("alpha", "rug screens pass", "token", 1.0, 1.10 + (i % 3) * 0.01) for i in range(40)])
    assert es.size_for(conn, "smart_money", {}) > 2.5
    monkeypatch.setenv("NOEMA_UNPROVEN_MODE", "shadow")
    assert es.size_for(conn, "news_latency", {}) == 0.0


def test_negative_forecast_score_vetoes(tmp_path):
    conn = _book(tmp_path)
    conn.execute("CREATE TABLE accuracy_forecasts (id INTEGER, agent_id TEXT)")
    conn.execute("CREATE TABLE accuracy_resolutions (forecast_id INTEGER, ev_after_cost REAL, brier REAL)")
    for i in range(40):
        conn.execute("INSERT INTO accuracy_forecasts VALUES (?, 'alpha')", (i,))
        conn.execute("INSERT INTO accuracy_resolutions VALUES (?, -0.05, 0.3)", (i,))
    _feed(tmp_path / "opportunity_feed.db",
          [("alpha", "rug screens pass", "token", 1.0, 1.12) for _ in range(40)])
    p = ps.strategy_proof(conn)
    assert p["smart_money"]["tier"] == "probe" and "alpha" in p["smart_money"]["vetoes"]
