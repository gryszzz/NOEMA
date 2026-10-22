from noema import trench_hft as h


def test_curve_round_trip_costs_two_fees():
    toks = h.buy_tokens(28.0, 0.05)
    back = h.sell_sol(28.0 * 1.0, toks)
    assert 0.97 * 0.05 < back < 0.99 * 0.05  # ~2% fees, tiny impact


def test_simulate_no_lookahead_and_costs():
    tr = [(100.0 + i, True, 0.1, f"w{i}", 28.0 + i * 0.01) for i in range(50)]
    coin = {"t0": 100.0, "creator": "c", "tr": tr, "smart": set()}
    r = h.simulate(coin, 110.0, 1.5, 5.0, 105.0, 0.0003, h.EXIT_RULES[0])
    assert r is not None and r < 0.02
    assert h.features(coin, 15)["uniq_buyers"] == 16
