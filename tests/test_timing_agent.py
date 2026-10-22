import json

from noema import timing_agent as ta


def _cs(prices, t0=1_000_000, vol=1000.0):
    out = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        out.append((t0 + 60 * i, o, max(o, p) * 1.005, min(o, p) * 0.995, p, vol))
    return out


def test_features_no_lookahead():
    cs = _cs([1.0] * 20 + [5.0] * 5)
    f = ta.features(cs, 10, 1.0, 1e6)
    assert f["impulse"] < 0.05


def test_spike_veto_blocks_entry():
    g = ta.Genome(mode="breakout", vol_mult=0.0, ofi_min=-1, spike_max=0.1)
    cs = _cs([1.0] * 15 + [1.5, 1.6])
    f = ta.features(cs, len(cs) - 1, 1.0, 1e6)
    assert ta.trigger(f, g)[0] is None


def test_no_trade_thin_pool():
    f = ta.features(_cs([1.0] * 20), 19, 1.0, 100.0)
    assert ta.no_trade(f, ta.Genome()) == "pool too thin (slippage)"


def test_manage_hard_stop_and_costs():
    cs = _cs([1.0] * 10 + [0.8] * 5)
    k, gross, why, _ = ta.manage(cs, 5, 1.0, ta.Genome(stop=-0.07))
    assert why == "hard stop" and gross < -0.06


def test_run_token_and_board(tmp_path):
    cs = _cs([1.0] * 10 + [1.1, 1.2, 1.15, 1.1, 1.12, 1.2, 1.3, 1.4] + [1.4] * 100)
    r = ta.run_token(cs, cs[10][0], 1.0, 1e6, ta.Genome(mode="either", ofi_min=-1, min_vol5_usd=0))
    assert set(r) >= {"traded", "net", "legs"}
    db = str(tmp_path / "t.db")
    c = ta.connect(db)
    import random
    ta.ensure_roster(c, random.Random(1))
    ta.lifecycle(c, random.Random(1))
    b = ta.board(db)
    assert b["paper_only"] and len(b["agents"]) == ta.POP
    json.dumps(b)


def test_walk_forward_shape():
    s = [{"t0": 1_000_000 + i * 600, "px": 1.0, "liq": 5e4,
          "cs": _cs([1.0] * 10 + [1.0 + 0.01 * ((i + j) % 7) for j in range(60)], t0=1_000_000 + i * 600 - 600)} for i in range(15)]
    rep = ta.walk_forward(s, folds=2)
    assert "oos_paired_diff" in rep and rep["genomes_tested"] >= 6
