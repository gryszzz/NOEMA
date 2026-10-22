import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from noema import adaptive_exits as ax
from noema import agent_lifecycle as al
from noema import edge_mining as em


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(em, "BOOT", 200)


def _db(path, n=400, start=None, good_liq=True):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE trench_candidates (candidate_id TEXT PRIMARY KEY, token_mint TEXT, captured_at TEXT,"
              " reference_price_usd REAL, reference_liquidity_usd REAL, disposition TEXT, survival_risk REAL,"
              " opportunity_score REAL, assessment_json TEXT)")
    c.execute("CREATE TABLE trench_counterfactuals (candidate_id TEXT, horizon_seconds INTEGER, final_return_fraction REAL,"
              " max_return_fraction REAL, max_drawdown_fraction REAL, observed_at TEXT)")
    t0 = start or datetime.now(UTC)
    rng = random.Random(1)
    for i in range(n):
        liq = 20000.0 if i % 2 else 500.0
        ret = -0.5 + rng.uniform(-0.05, 0.05)  # everything loses
        at = (t0 + timedelta(seconds=i)).isoformat()
        c.execute("INSERT INTO trench_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                  (str(i), f"m{i}", at, 1.0, liq, "observe", 0.3, 0.5,
                   json.dumps({"features": {"age_seconds": 100 + i}})))
        c.execute("INSERT INTO trench_counterfactuals VALUES (?,?,?,?,?,?)", (str(i), 3600, ret, 0, 0, at))
    c.commit()
    c.close()


def test_net_return_costs_and_cap():
    assert abs(em.net_return(100.0, 1e9) - (em.RETURN_CAP - em.FEE_ROUND_TRIP)) < 1e-6
    assert em.net_return(0.0, 50.0) < -0.5


def test_bh_adjust_monotone():
    adj = em.bh_adjust([0.01, 0.04, 0.03, 0.5])
    assert adj[0] <= adj[2] <= adj[1] <= adj[3] <= 1


def test_nothing_survives_on_losing_data(tmp_path):
    _db(tmp_path / "n.db")
    r = em.mine_all(str(tmp_path / "n.db"))
    assert r["survivors"] == [] and "nothing survived" in r["verdict"]


def test_fire_postmortem_and_replacement(tmp_path):
    db, life = str(tmp_path / "n.db"), str(tmp_path / "l.db")
    c = al.connect(life)
    al.seed(c, {"families": [{"survivors": [], "best_train_rules": []}]})
    c.execute("UPDATE agents SET hired_at='2000-01-01'")
    c.commit()
    c.close()
    _db(db)
    al.cycle(db, life, rng=random.Random(3))
    r = al.roster(life)
    fired = [a for a in r["agents"] if a["status"] == "fired"]
    assert fired and fired[0]["postmortem"]["summary"]
    kids = [a for a in r["agents"] if a["origin"] == "replacement"]
    assert kids and kids[0]["status"] == "shadow" and kids[0]["generation"] == 1 and kids[0]["weight"] == 0
    assert len([a for a in r["agents"] if a["status"] != "fired"]) <= al.POP_CAP


def test_new_hire_only_scored_prospectively(tmp_path):
    db, life = str(tmp_path / "n.db"), str(tmp_path / "l.db")
    _db(db, start=datetime.now(UTC) - timedelta(days=1))
    al.cycle(db, life)
    r = al.roster(life)
    assert all(a["stats"].get("n", 0) == 0 and a["status"] == "shadow" for a in r["agents"])


def _path(prices, liqs=None, vols=None):
    hs = [0, 0.25, 0.5, 1, 2, 4, 6][:len(prices)]
    return [{"h": h, "price": p, "liq": (liqs or [1e4] * 9)[i], "vol": (vols or [100] * 9)[i]}
            for i, (h, p) in enumerate(zip(hs, prices))]


def test_runner_rides_while_old_rules_sell_out():
    path = _path([1, 1.35, 1.8, 2.6, 4.0, 6.0, 7.0])
    assert ax.new_rules_return(path, 0) > ax.old_rules_return(path, 0)


def test_break_even_after_tp1_and_liquidity_pull():
    st = ax.PosState(entry=1.0, last_price=1.0)
    p = ax.AdaptiveExit()
    f, why = ax.decide(st, p, price=1.31, age_h=0.1)
    assert why.startswith("take profit 1") and abs(f - 0.5) < 1e-9
    f, why = ax.decide(st, p, price=0.99, age_h=0.2)
    assert why == "break-even stop" and abs(f - 0.5) < 1e-9
    st = ax.PosState(entry=1.0, last_price=1.0, liq_high=1e4)
    assert ax.decide(st, p, price=1.0, age_h=0.1, liquidity=5e3)[1] == "liquidity pulled"


def test_time_stop_extends_only_with_momentum():
    p = ax.AdaptiveExit(time_stop_h=1, extend_h=1)
    st = ax.PosState(entry=1.0, last_price=1.05)
    assert ax.decide(st, p, price=1.1, age_h=1.0, volume=100)[1] is None and st.extended_h == 1
    st2 = ax.PosState(entry=1.0, last_price=1.05)
    assert ax.decide(st2, p, price=0.98, age_h=1.0)[1] == "time stop"


def test_setup_universe_blocks_majors_and_cools_down(tmp_path):
    from noema import setup_universe as su
    cfg = su.load_config()
    assert su.is_major("SOL", None, None, cfg) and su.is_major("X", 5e9, "meme", cfg)
    assert not su.is_major("NEWCOIN", 2e6, "meme", cfg)
    fc = sqlite3.connect(tmp_path / "f.db")
    now = datetime.now(UTC)
    assert su.novel(fc, "e", now, cfg, volume=10, liquidity=1e4, price=1, catalyst=None) == "new"
    assert su.novel(fc, "e", now, cfg, volume=11, liquidity=1e4, price=1.05, catalyst=None) is None
    assert su.novel(fc, "e", now, cfg, volume=30, liquidity=1e4, price=1, catalyst=None).startswith("volume")
    assert su.novel(fc, "e", now + timedelta(hours=7), cfg, volume=30, liquidity=1e4, price=1, catalyst=None)
