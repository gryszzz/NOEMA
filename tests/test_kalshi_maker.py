import json
import sqlite3

import pytest

from noema import kalshi_maker as km


def test_fees_round_up_to_centicent():
    assert km.maker_fee(0.5) == pytest.approx(0.0044)  # 0.0175*0.25 = 0.004375 -> 0.0044
    assert km.maker_fee(0.5, m=0) == 0.0
    assert km.taker_fee(0.5) == pytest.approx(0.0175)
    assert km.maker_fee(0.01) == pytest.approx(0.0002)


def test_decide_join_and_improve():
    assert km.decide(0.60, 0.50, 0.52, "join") == ("yes", 0.50)
    assert km.decide(0.40, 0.50, 0.52, "join") == ("no", 0.52)
    assert km.decide(0.502, 0.50, 0.505, "join") is None  # both edges < 0.44c maker fee
    assert km.decide(0.60, 0.50, 0.53, "improve") == ("yes", 0.51)
    assert km.decide(0.60, 0.50, 0.51, "improve") is None  # 1c spread: nothing inside
    assert km.decide(0.60, None, 0.51, "join") is None


def test_net_pnl_includes_fee_and_penalty():
    assert km.net_pnl("yes", 0.5, 1) == pytest.approx(1 - 0.5 - 0.0044 - 0.005)
    assert km.net_pnl("no", 0.6, 1) == pytest.approx(-0.4 - km.maker_fee(0.4) - 0.005)


def _ev():
    ev = km.Evidence()
    for i, (ts, px) in enumerate([(5, 0.50), (10, 0.50), (20, 0.49), (40, 0.55)]):
        ev.add_trade(str(i), "T", ts, px)
    ev.finish()
    return ev


def test_fill_requires_trade_through_last_in_queue():
    ev = _ev()
    assert km.Evidence.fill(ev, "T", "yes", 0.50, 0, 15) == (True, False)  # at-price prints never fill
    assert ev.fill("T", "yes", 0.50, 0, 15, at_price=True) == (True, True)
    assert ev.fill("T", "yes", 0.50, 0, 25) == (True, True)  # 0.49 prints through
    assert ev.fill("T", "no", 0.54, 0, 30) == (True, False)
    assert ev.fill("T", "no", 0.54, 0, 45) == (True, True)
    assert ev.fill("X", "yes", 0.50, 0, 45) == (False, False)  # no evidence = not evaluable


def test_truncated_history_not_used_before_first_print():
    ev = _ev()
    ev.trade_start["T"] = 5
    assert ev.fill("T", "yes", 0.50, 0, 25) == (False, False)
    assert ev.fill("T", "yes", 0.50, 6, 25) == (True, True)


def test_partial_prints_prove_fills_only_and_quotes_cross():
    ev = km.Evidence()
    ev.partial["P"] = [(10, 0.40)]
    ev.quotes["Q"] = [(10, 0.45, 0.47), (20, 0.38, 0.39)]
    ev.finish()
    assert ev.fill("P", "yes", 0.45, 0, 30) == (True, True)
    assert ev.fill("P", "yes", 0.35, 0, 30) == (False, False)
    assert ev.fill("Q", "yes", 0.45, 0, 15) == (True, False)
    assert ev.fill("Q", "yes", 0.45, 0, 25) == (True, True)


def test_holm_and_verdicts():
    assert km.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    good = {"stats": {"n": 400, "events": 80, "mean": 0.02, "ci95": [0.005, 0.03], "p_one_sided": 0.0005},
            "by_day": {"a": {"n": 100, "mean": 0.01}, "b": {"n": 150, "mean": 0.02}, "c": {"n": 150, "mean": 0.03}}}
    small = {"stats": {"n": 40, "events": 10, "mean": 0.02, "ci95": [-0.01, 0.05], "p_one_sided": 0.2},
             "by_day": {"a": {"n": 40, "mean": 0.02}}}
    bad = {"stats": {"n": 400, "events": 80, "mean": -0.02, "ci95": [-0.03, -0.01], "p_one_sided": 1.0},
           "by_day": {"a": {"n": 400, "mean": -0.02}}}
    rs = [good, small, bad]
    km.verdicts(rs)
    assert [r["verdict"] for r in rs] == ["PASS", "promising, under-sampled", "fail"]


def test_cluster_boot_deterministic():
    vals = [(f"e{i % 10}", 0.01 * (i % 3)) for i in range(60)]
    a, b = km.cluster_boot(vals), km.cluster_boot(vals)
    assert a == b and a["events"] == 10 and a["ci95"][0] - 1e-9 <= a["mean"] <= a["ci95"][1] + 1e-9


def test_forward_once_paper_only(tmp_path):
    main = tmp_path / "noema.db"
    c = sqlite3.connect(main)
    c.executescript("""
    CREATE TABLE fs_markets (ticker TEXT, event_ticker TEXT, close_time TEXT, result TEXT);
    CREATE TABLE accuracy_forecasts (agent_id TEXT, market_id TEXT, recorded_at TEXT, predicted_probability REAL,
        metadata_json TEXT, venue TEXT);
    CREATE TABLE fs_trades (trade_id TEXT, ticker TEXT, created_time TEXT, yes_price REAL);
    CREATE TABLE fs_quotes (ticker TEXT, at TEXT, bid REAL, ask REAL);
    CREATE TABLE fs_books (ticker TEXT, at TEXT, best_bid REAL, best_ask REAL);
    """)
    c.execute("INSERT INTO fs_markets VALUES ('K1','E1','2026-10-23T03:00:00+00:00','yes')")
    c.execute("INSERT INTO accuracy_forecasts VALUES (?,?,?,?,?,?)",
              ("pm-weather-market_cal-b2469c", "K1", "2026-10-23T02:30:00+00:00", 0.7,
               json.dumps({"bid": 0.5, "ask": 0.52}), "kalshi"))
    c.commit()
    c.close()

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"trades": [{"trade_id": "x1", "created_time": "2026-10-23T02:40:00Z", "yes_price_dollars": "0.48",
                                "count_fp": "3", "taker_side": "no"}], "cursor": ""}

    class Client:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None):
            assert url.endswith("/markets/trades")  # read-only public prints, nothing else
            self.calls.append(params)
            return Resp()

    from datetime import UTC, datetime
    mk = tmp_path / "km.db"
    cl = Client()
    rep = km.forward_once(str(main), str(mk), None, client=cl, now=datetime(2026, 10, 23, 2, 0, tzinfo=UTC))
    assert rep["window"][0].startswith("2026-10-23T02:00") and rep["window"][1].startswith("2026-10-30T02:00")
    assert rep["cases"] == 1 and not rep["final"]
    r = next(x for x in rep["results"] if x["agent"] == "pm-weather-market_cal-b2469c" and x["variant"] == "join")
    assert r["stats"]["n"] == 1 and r["stats"]["mean"] == pytest.approx(1 - 0.5 - 0.0044 - 0.005, abs=1e-4)
    rep2 = km.forward_once(str(main), str(mk), None, client=cl, now=datetime(2026, 10, 23, 2, 15, tzinfo=UTC))
    assert len(cl.calls) == 1 and rep2["window"] == rep["window"]  # prints fetched once; window fixed
    rep3 = km.forward_once(str(main), str(mk), None, client=cl, now=datetime(2026, 10, 31, 3, 0, tzinfo=UTC))
    assert rep3["final"]
    rep4 = km.forward_once(str(main), str(mk), None, client=cl, now=datetime(2026, 11, 5, tzinfo=UTC))
    assert rep4 == rep3
