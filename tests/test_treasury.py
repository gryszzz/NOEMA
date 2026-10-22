import json
import sqlite3
from datetime import UTC, datetime

from noema.treasury import treasury_board


def test_treasury_separates_real_paper_and_deposits(tmp_path):
    db = str(tmp_path / "t.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE equity_feed_readings (at TEXT, status TEXT, total_usd TEXT, deposit_usd TEXT, sources_json TEXT)")
    c.execute("CREATE TABLE equity_feed_deposits (at TEXT, source TEXT, asset TEXT, quantity TEXT, amount_usd TEXT)")
    c.execute("CREATE TABLE capital_guard (id INTEGER, peak_equity TEXT, floor TEXT, equity TEXT, day TEXT, "
              "day_start_equity TEXT, tripped INTEGER, trip_reason TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE trench_desk_events (id INTEGER PRIMARY KEY, ts TEXT, event TEXT, payload_json TEXT)")
    src = {"solana": {"status": "ok", "equity_usd": "6"}, "evm_base": {"status": "ok", "equity_usd": "1"},
           "kalshi": {"status": "ok", "equity_usd": "4"}, "polymarket_us": {"status": "error"}}
    now = datetime.now(UTC)
    for at, tot in (("2026-10-21T20:00:00+00:00", "1"), (now.isoformat(), "11")):
        c.execute("INSERT INTO equity_feed_readings VALUES (?,?,?,?,?)", (at, "complete", tot, "0", json.dumps(src)))
    c.execute("INSERT INTO equity_feed_deposits VALUES ('2026-10-21T21:00:00+00:00','solana','SOL','1','10')")
    c.execute("INSERT INTO capital_guard VALUES (1,'11','8.25','11','d','10',0,NULL,'x')")
    c.execute("INSERT INTO trench_desk_events (ts,event,payload_json) VALUES (?,?,?)",
              (now.isoformat(), "equity_snapshot", '{"equity_usd": 700}'))
    c.commit()
    d = treasury_board(db, now=now)
    assert d["real"]["total_usd"] == 11 and d["real"]["pnl_all_usd"] == 0  # deposit is not profit
    v = {x["key"]: x for x in d["real"]["venues"]}
    assert v["solana"]["usd"] == 6 and v["evm"]["usd"] == 1 and v["polymarket_us"]["status"] == "partial"
    assert d["guard"]["daily_stop_usd"] == 9.0 and d["guard"]["pnl_today_usd"] == 1
    assert d["paper"]["desks"][0]["usd"] == 700 and "not real" in d["paper"]["label"]
