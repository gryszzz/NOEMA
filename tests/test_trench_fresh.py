import sqlite3
from datetime import UTC, datetime, timedelta

from noema.trench_desk_runtime import chief_gate
from noema.trench_fresh import FreshConfig, FreshLaunchRunner, evaluate
from noema.trench_judgement import Judgement, ledger_confidence, with_ledger_confidence


def _ledger(conn, agent, rows):
    conn.executescript("""
    CREATE TABLE accuracy_forecasts (id INTEGER PRIMARY KEY, recorded_at TEXT, agent_id TEXT, venue TEXT,
      market_id TEXT, symbol TEXT, predicted_probability REAL, confidence REAL, market_price REAL,
      reasoning_hash TEXT, decision TEXT, horizon TEXT, metadata_json TEXT);
    CREATE TABLE accuracy_resolutions (forecast_id INTEGER PRIMARY KEY, resolved_at TEXT, outcome REAL,
      source TEXT, brier REAL, log_loss REAL, hit INTEGER, edge REAL, ev_after_cost REAL, postmortem TEXT);""")
    for i, (p, o) in enumerate(rows):
        conn.execute("INSERT INTO accuracy_forecasts (id, agent_id, predicted_probability) VALUES (?,?,?)",
                     (i, agent, p))
        conn.execute("INSERT INTO accuracy_resolutions (forecast_id, resolved_at, outcome) "
                     "VALUES (?, datetime('now'), ?)", (i, o))


def test_ledger_confidence_rewards_calibration_and_sample_size():
    good, bad = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    _ledger(good, "a", [(0.8, 1)] * 80 + [(0.2, 0)] * 80)
    _ledger(bad, "a", [(0.85, 0)] * 13 + [(0.85, 1)] * 5)
    g, b = ledger_confidence(good, "a"), ledger_confidence(bad, "a")
    assert g["confidence"] > 0.55 > b["confidence"]
    assert ledger_confidence(sqlite3.connect(":memory:"), "a")["confidence"] == 0.0


def test_rules_picks_get_a_confidence_and_chief_gives_a_real_reason():
    conn = sqlite3.connect(":memory:")
    _ledger(conn, "trench_rules", [(0.85, 0)] * 13 + [(0.85, 1)] * 5)
    picks = with_ledger_confidence(conn, [Judgement("m", True, 0.85, "rules")], "trench_rules")
    assert picks[0].confidence is not None
    approved, log = chief_gate(picks)
    assert not approved and log[0]["reason"].startswith("confidence=")


def _f(**kw):
    base = {"uniq_buyers": 30, "top3_share": 0.3, "first1s_share": 0.1, "ofi": 0.5, "progress": 0.2,
            "ret_10s": 0.05, "dd_peak": -0.05, "dev_sold": 0.0, "buyer_rate": 0.8}
    return base | kw


def test_evaluate_enter_skip_drop():
    cfg = FreshConfig()
    assert evaluate(_f(), 45, cfg)[0] == "enter"
    assert evaluate(_f(uniq_buyers=3), 15, cfg)[0] == "skip"
    assert evaluate(_f(dev_sold=1.0), 15, cfg) == ("drop", ["dev sold"])
    assert evaluate(_f(ofi=-0.2), 45, cfg)[0] == "drop"
    assert evaluate(None, 120, cfg)[0] == "drop"


def test_runner_paper_enters_and_exits(tmp_path):
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.executescript("""CREATE TABLE launch_stream (mint TEXT PRIMARY KEY, signature TEXT, received_at TEXT,
      source TEXT, name TEXT, symbol TEXT, creator TEXT, initial_sol REAL, market_cap_sol REAL, raw_json TEXT);
      CREATE TABLE launch_trades (signature TEXT PRIMARY KEY, mint TEXT, received_at TEXT, side TEXT, sol REAL,
      tokens REAL, trader TEXT, market_cap_sol REAL, source TEXT);""")
    now = datetime.now(UTC)
    t0 = now - timedelta(seconds=50)
    conn.execute("INSERT INTO launch_stream (mint, received_at, source, symbol, creator) VALUES "
                 "('Xpump', ?, 'test', 'X', 'dev')", (t0.isoformat(),))
    for i in range(40):
        conn.execute("INSERT INTO launch_trades VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"s{i}", "Xpump", (t0 + timedelta(seconds=2 + i)).isoformat(), "buy", 0.2, 1,
                      f"w{i}", 30 + i * 0.5, "test"))
    conn.commit()
    runner = FreshLaunchRunner(db, FreshConfig(enabled=True))
    s = runner.tick(sol_usd=150.0, now=now)
    assert s["prefilter_passed"] == 1 and s["entries"] == 1
    trades = conn.execute("SELECT paper, side FROM trench_desk_trades").fetchall()
    assert trades and all(p == 1 for p, _ in trades)
    s2 = runner.tick(sol_usd=150.0, now=now + timedelta(seconds=200))
    assert s2["exits"] == 1
    ev = conn.execute("SELECT payload_json FROM trench_desk_events WHERE event='fresh_decision'").fetchall()
    assert ev and "enter" in ev[0][0]
