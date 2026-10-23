from noema import trench_edges as e
from noema import trench_hft as h
from noema import trench_specs as s


def test_mc_for_progress_round_trips_curve():
    assert abs(h.v_from_mc(s.mc_for_progress(0.0)) - 30) < 1e-6
    assert abs((h.v_from_mc(s.mc_for_progress(0.95)) - 30) / h.CURVE_SOL - 0.95) < 1e-6


def test_originality_uses_only_earlier_launches():
    rows = [(1.0, "a", "dev1", "pepe|pepe", 1.0), (2.0, "b", "dev1", "pepe|pepe", 5.0), (3.0, "c", "dev2", "x|x", 0.1)]
    o = s.originality({"a", "b", "c"}, rows)
    assert o["a"] == {"creator_prior_n": 0, "name_sym_seen_before": False, "dev_init_sol": 1.0}
    assert o["b"]["creator_prior_n"] == 1 and o["b"]["name_sym_seen_before"] and o["b"]["dev_init_sol"] == 5.0
    assert o["c"]["creator_prior_n"] == 0 and not o["c"]["name_sym_seen_before"]


def test_manip_features_ignore_future_and_count_atomic_round_trips():
    ext = [(1.0, True, 10.0, "w", 30.0, "s1"), (1.0, False, 10.0, "w", 30.0, "s1"),
           (2.0, True, 5.0, "v", 30.0, "s2"), (9.0, False, 5.0, "v", 30.0, "s3")]
    f = s.manip_features(ext, 5.0)
    assert f["atomic_rt_n"] == 1 and f["wash_max"] == 1.0  # w flipped once, net 0
    assert s.manip_features(ext, 0.5) == {"wash_max": 0.0, "atomic_rt_n": 0}


def test_sim_exit_mc_sells_at_threshold():
    tr = [(100.0 + i, True, 0.1, "x", 30.0 + i) for i in range(60)]
    c = {"t0": 100.0, "creator": "d", "tr": tr, "smart": set(), "T": [x[0] for x in tr], "cov": 1e18}
    r_cap = e.sim(c, 100.0, 0.0, 5.0, 104.0, 0.0, {"tmax": 50, "exit_mc": 40.0})
    r_full = e.sim(c, 100.0, 0.0, 5.0, 104.0, 0.0, {"tmax": 50})
    assert r_cap is not None and r_full is not None and r_cap < r_full
