import random

from noema import pm_models as pm


def test_fee_and_weather_prob():
    assert abs(pm.kalshi_fee(0.5) - 0.0175) < 1e-9
    assert pm.weather_prob(70, "between", 69, 70) > pm.weather_prob(70, "between", 60, 61)
    assert pm.weather_prob(70, "greater", 60, None) > 0.95


def test_logistic_recovers_signal_and_bh():
    rng = random.Random(1)
    xs, ys = [], []
    for _ in range(800):
        p = rng.uniform(0.05, 0.95)
        xs.append([1.0, pm.logit(p)])
        ys.append(int(rng.random() < p))
    w = pm.fit_logistic(xs, ys, 1.0, [0.0, 1.0])
    assert abs(w[0]) < 0.3 and 0.7 < w[1] < 1.3
    assert pm.bh([0.001, 0.5, 0.04]) == [True, False, False]


def test_walk_forward_uses_only_earlier_days():
    rows = []
    for d in range(1, 11):
        for i in range(40):
            mid = 0.3 if i % 2 else 0.7
            rows.append({"category": "c", "day": f"2026-09-{d:02d}", "snap_mid": mid, "model_p": None,
                         "model_kind": None, "result": int(i % 2 == 0), "event_ticker": f"E{d}-{i % 5}",
                         "snap_bid": mid - 0.01, "snap_ask": mid + 0.01})
    preds = pm.walk_forward(rows, {"category": "c", "model": "market_cal", "window_days": 999, "ridge": 1.0},
                            min_train=100)
    assert preds and min(r["day"] for r, _ in preds) == "2026-09-04"  # first 3 days are training only
    ev = pm.evaluate(preds)
    assert ev["brier_minus_market"] < 0  # calibration learns the market is under-confident here


def test_bars_never_use_an_unfinished_bar():
    b = pm.Bars([(0, 100.0), (300, 101.0), (600, 102.0)])
    assert b.spot(599)[0] == 100.0  # bar starting at 300 only closes at 600
    assert b.spot(900)[0] == 102.0
