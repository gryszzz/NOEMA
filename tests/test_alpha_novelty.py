import sqlite3
from datetime import UTC, datetime, timedelta

from noema import alpha_discovery as ad


def test_alpha_rotates_fresh_tokens_and_ages_out_replays():
    c = sqlite3.connect(":memory:")
    ad.ensure_schema(c)
    c.execute("CREATE TABLE culture_signals (entity TEXT, symbol TEXT, chain TEXT, kind TEXT, buzz_velocity REAL,"
              " score REAL, updated_at TEXT)")
    t0 = datetime(2026, 10, 21, 20, tzinfo=UTC)
    c.executemany("INSERT INTO culture_signals VALUES (?,?,?,?,?,?,?)",
                  [(f"solana:A{i}", f"A{i}", "solana", "token", 1.0, 100 - i, t0.isoformat()) for i in range(10)])
    first = [x.address for x in ad._candidates_from_signals(c, t0, 4)]
    assert first == ["A0", "A1", "A2", "A3"]
    second = [x.address for x in ad._candidates_from_signals(c, t0 + timedelta(minutes=5), 4)]
    assert second[0] not in first  # newly seen tokens rotate in ahead of repeats
    # 3h later the old ones are aged out unless buzz re-triggers
    c.execute("UPDATE culture_signals SET updated_at=?", ((t0 + timedelta(hours=3)).isoformat(),))
    c.execute("UPDATE culture_signals SET buzz_velocity=5 WHERE entity='solana:A0'")
    st: dict = {}
    third = [x.address for x in ad._candidates_from_signals(c, t0 + timedelta(hours=3), 4, st)]
    assert "A0" in third and "A1" not in third and st["aged_out"] >= 1
