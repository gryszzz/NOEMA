import random

from noema import trench_edges as e
from noema import trench_hft as h


def _coin(n=120, step=1.0, drift=0.02, seed=3):
    rnd = random.Random(seed)
    mc, tr = 28.0, []
    for i in range(n):
        mc = max(1.0, mc * (1 + rnd.uniform(-0.05, 0.05) + drift / 10))
        tr.append((100.0 + i * step, rnd.random() < 0.6, 0.1, f"w{i % 17}", mc))
    c = {"t0": 100.0, "creator": "dev", "tr": tr, "smart": set()}
    c["T"] = [x[0] for x in tr]
    c["cov"] = c["t0"] + e.COVER_S
    return c


def test_generic_sim_matches_trench_hft_simulate():
    c = _coin()
    c["cov"] = 1e18
    for rule in h.EXIT_RULES:
        a = h.simulate(c, 110.0, 1.5, 5.0, 104.0, 0.0003, rule)
        b = e.sim(c, 110.0, 1.5, 5.0, 104.0, 0.0003, e.hft_rule(rule))
        assert a is not None and abs(a - b) < 1e-9


def test_sim_refuses_windows_past_coverage():
    c = _coin()
    assert e.sim(c, 110.0, 1.5, 5.0, 104.0, 0.0003, {"tmax": 60}) is not None
    assert e.sim(c, 110.0, 1.5, 5.0, 104.0, 0.0003, {"tmax": 160}) is None  # would mark a frozen price


def test_flat_curve_loses_fees():
    tr = [(100.0 + i, True, 0.0, "w", 30.0) for i in range(100)]
    c = {"t0": 100.0, "creator": None, "tr": tr, "smart": set(), "T": [x[0] for x in tr], "cov": 270.0}
    r = e.sim(c, 110.0, 1.5, 5.0, 104.0, 0.0003, {"tmax": 30})
    assert -0.05 < r < -0.02  # 2 x 1% curve fee + impact + 2 priority fees


def test_ladder_and_trail():
    c = _coin(drift=0.5)
    rule = {"tmax": 60, "sl": 0.3, "ladder": [(0.1, 0.5)], "trail": 0.1, "trail_after_tp": True}
    assert e.sim(c, 105.0, 1.0, 5.0, 104.0, 0.0003, rule) is not None


def test_folds_are_time_ordered():
    coins = {str(i): {"t0": float(i)} for i in range(100)}
    edges = e.fold_edges(coins)
    assert edges == sorted(edges) and len(edges) == e.FOLDS - 1
    assert e.fold_of(0.0, edges) == 0 and e.fold_of(99.0, edges) == e.FOLDS - 1


def test_verdict_requires_heldout():
    d = {"n": 100, "mean": 0.1, "p": 0.0001}
    assert not e.verdict(d, 10, {"n": 10, "mean": 0.5, "p": 0.01})["PASS"]
    assert e.verdict(d, 10, {"n": 40, "mean": 0.05, "p": 0.01})["PASS"]


def test_gap_guard_cuts_coverage_and_drops_coins_born_in_outage():
    def coin(t0, ts):
        return {"t0": t0, "tr": [(t, True, 0.1, "w", 30.0) for t in ts], "T": list(ts), "cov": t0 + 170}
    coins = {"a": coin(0, [0, 1, 2, 3, 4, 5]), "b": coin(5, [5, 6]), "c": coin(40, [40, 41, 42])}
    # global tape: 0..6 then silence until 40 -> outage (6, 40); c created at 40 (gap end), a/b cut at 6
    out = e.apply_gap_guard(coins, gap_s=10)
    assert out["a"]["cov"] == 6 and out["b"]["cov"] == 6
    assert "c" in out
    coins["d"] = coin(20, [])
    assert "d" not in e.apply_gap_guard(coins, gap_s=10)


def test_creator_state_uses_only_known_outcomes():
    hist = [(0.0, 3600.0, False), (100.0, 3700.0, True)]
    assert e.creator_state(hist, 50.0) == (0, 0)
    assert e.creator_state(hist, 3650.0) == (1, 0)
    assert e.creator_state(hist, 3700.0) == (2, 1)
