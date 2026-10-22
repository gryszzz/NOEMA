import sqlite3

from noema import edge_strategies as es
from noema import xv_match


def test_gate_default_and_env(monkeypatch):
    monkeypatch.delenv("NOEMA_ENTRY_BUFFER", raising=False)
    assert not es.clears_fee_buffer(None)
    assert not es.clears_fee_buffer(0.005)
    assert es.clears_fee_buffer(0.01)
    monkeypatch.setenv("NOEMA_ENTRY_BUFFER", "0.03")
    assert not es.clears_fee_buffer(0.02)


def test_gap_stats_counts_only_equivalent_synced():
    c = sqlite3.connect(":memory:")
    xv_match.ensure_schema(c)
    rows = [("t1", "a", "k", "equivalent", 1.0, 0.03, 0.97, 0.02),
            ("t1", "b", "k2", "equivalent", 1.0, 0.0, 1.02, -0.02),
            ("t2", "a", "k", "caveat", 1.0, 0.05, 0.95, 0.04),
            ("t2", "b", "k2", "equivalent", 500.0, 0.05, 0.95, 0.04)]
    c.executemany("INSERT INTO xv_gap_history VALUES (?,?,?,?,?,?,?,?)", rows)
    s = xv_match.gap_stats(c, buffer=0.01)
    assert s["pair_observations"] == 2 and s["observations_clearing"] == 1 and s["max_edge"] == 0.02
