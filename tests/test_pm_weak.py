from noema import pm_weak as w


def test_event_day_and_band_prob():
    assert w.event_day("KXHIGHNY-26SEP06").day == 6
    p_mid = w._band_prob(75.5, 2.0, "between", 75, 76)
    p_far = w._band_prob(70.0, 2.0, "between", 75, 76)
    assert p_mid > p_far
    assert abs(w._band_prob(80, 2, "greater", 79, None) - (1 - w._band_prob(80, 2, "less", None, 80))) < 1e-9


def test_walk_forward_trains_only_on_markets_closed_before_snapshot():
    rows = [{"snap_ts": i, "close_ts": i + 1000, "y": i % 2, "mid": 0.5} for i in range(400)]
    w.walk_forward(rows, lambda r: [1.0, 0.0], min_train=50)
    # nothing closed before snap 999 -> no model; first fitted prediction appears only after 50 closes
    assert all(r["p_wf"] is None for r in rows if r["snap_ts"] < 1050)


def test_evaluate_gate_and_annualised():
    rows = [{"event": f"e{i}", "y": 1, "bid": 0.30, "ask": 0.32, "mid": 0.31, "p": 0.6, "days": 30} for i in range(10)]
    e = w.evaluate(rows)
    assert e["paper_trades"] == 10 and e["pnl_per_contract"] > 0 and e["annualised_return"] > 0
    rows2 = [dict(r, p=0.325) for r in rows]  # edge < fee + 1c buffer -> no trade
    assert w.evaluate(rows2)["paper_trades"] == 0
