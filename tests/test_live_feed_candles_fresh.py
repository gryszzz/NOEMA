import sqlite3
import time
from datetime import UTC, datetime

from noema.live_feed import candles_section


def _iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


def test_candles_window_drops_aged_out_and_dupes(tmp_path):
    db = str(tmp_path / "x.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE launch_stream(mint, name, symbol, received_at)")
    c.execute("CREATE TABLE launch_trades(mint, received_at, market_cap_sol, sol)")
    now = time.time()
    for mint, age in (("new", 60), ("old", 7200), ("new", 30)):
        c.execute("INSERT INTO launch_stream VALUES(?,?,?,?)", (mint, mint, mint, _iso(now - age)))
    for mint in ("new", "old"):
        for i in range(15):
            c.execute("INSERT INTO launch_trades VALUES(?,?,?,?)", (mint, _iso(now - 50 + i), 30 + i, 0.1))
    c.commit()
    c.close()
    out = candles_section(db, now)
    assert [x["mint"] for x in out["charts"]] == ["new"]
