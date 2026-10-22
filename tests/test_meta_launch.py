import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from noema import agent_lifecycle as al
from noema import edge_mining as em
from noema import meta_launch as ml

T0 = datetime(2026, 10, 21, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(em, "BOOT", 200)


def _launch(mint, name, sym, **audit):
    a = {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "devMints": 1,
         "devBalancePercentage": 1, "topHoldersPercentage": 10, **audit}
    return {"mint": mint, "at": T0, "name": name, "symbol": sym, "terms": ml.tokens(f"{name} {sym}"),
            "d": {"audit": a, "holderCount": 50, "liquidity": 8000, "stats5m": {"numTraders": 40, "numBuys": 30}}}


def test_tokens_drop_noise():
    assert ml.tokens("The KIMCHI Inu coin $KIMCHI") == {"kimchi"}


def test_shield_rejects_insiders():
    ok, why, _ = ml.shield(_launch("a", "x", "x"), {}, {})
    assert ok and not why
    assert not ml.shield(_launch("b", "x", "x", devMints=50), {}, {})[0]
    assert not ml.shield(_launch("c", "x", "x", devBalancePercentage=40), {}, {})[0]
    assert not ml.shield(_launch("d", "x", "x", mintAuthorityDisabled=False), {}, {})[0]
    assert not ml.shield(_launch("e", "x", "x"), {}, {"e": ["early_buyers_dumped"]})[0]
    assert not ml.shield(_launch("f", "x", "x"), {"f": (5, 4, 0.9)}, {})[0]


def test_surge_needed_common_words_ignored():
    clock = ml.TermClock(timedelta(minutes=30))
    for i in range(72):  # 'cat' is always there: 6 per 30 min for 6h
        clock.add(T0 - timedelta(minutes=5 * i), "launch", {"cat"})
    for i in range(8):   # 'kimchi' bursts in the last 30 min, also on X
        clock.add(T0 - timedelta(minutes=i), "launch", {"kimchi"})
        clock.add(T0 - timedelta(minutes=i), "x", {"kimchi"})
    m = clock.snapshot(T0)
    assert "kimchi" in m and "cat" not in m
    assert m["kimchi"]["stage"] == "early" and m["kimchi"]["cross"]


def _db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE trench_launches (mint TEXT PRIMARY KEY, first_pool_at TEXT, discovered_at TEXT,"
              " last_seen_at TEXT, discovery_json TEXT, active INTEGER)")
    c.execute("CREATE TABLE trench_observations (mint TEXT, horizon_seconds INTEGER, scheduled_at TEXT,"
              " observed_at TEXT, tick_json TEXT, control_json TEXT, raw_token_json TEXT, holder_shares_json TEXT,"
              " provider_provenance_json TEXT)")
    c.execute("CREATE TABLE x_posts (post_id TEXT, created_at TEXT, text TEXT)")
    for i in range(12):
        at = T0 + timedelta(minutes=2 * i)
        mint = f"m{i}"
        d = _launch(mint, f"Kimchi Wave {i}", "KIM")["d"]
        d.update(name=f"Kimchi Wave {i}", symbol="KIM")
        c.execute("INSERT INTO trench_launches VALUES (?,?,?,?,?,1)", (mint, at.isoformat(), at.isoformat(),
                                                                         at.isoformat(), json.dumps(d)))
        c.execute("INSERT INTO x_posts VALUES (?,?,?)", (str(i), at.isoformat(), "kimchi is the new meta"))
        for h, px in ((30, 1.0), (300, 1.6), (3600, 2.0)):
            c.execute("INSERT INTO trench_observations VALUES (?,?,?,?,?, '{}', '{}', '[]', NULL)",
                      (mint, h, at.isoformat(), at.isoformat(),
                       json.dumps({"price_usd": px, "liquidity_usd": 8000, "buy_volume_usd": 100})))
    c.commit()
    c.close()


def test_replay_matches_first_vs_copycat_and_logs(tmp_path):
    db = str(tmp_path / "n.db")
    _db(db)
    c = ml._ro(db)
    matches, _ = ml.replay(c, T0, T0 + timedelta(minutes=40), step_min=2)
    c.close()
    assert matches and all(m["meta"] == "kimchi" for m in matches)
    assert sum(m["rank"] == "first" for m in matches) == 1
    assert all(m["net"] is not None for m in matches)
    out = ml.cycle(db, str(tmp_path / "meta.db"), now=T0 + timedelta(minutes=40), backtest_hours=1)
    assert out["matches"] == len(matches)
    b = ml.board(str(tmp_path / "meta.db"))
    assert b["mode"] == "paper only" and b["walk_forward"]["status"] == "insufficient_data"


def test_meta_genomes_join_roster(tmp_path):
    db = str(tmp_path / "n.db")
    _db(db)
    meta = str(tmp_path / "meta.db")
    ml.cycle(db, meta, now=T0 + timedelta(minutes=40), backtest_hours=1)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE trench_candidates (candidate_id TEXT, token_mint TEXT, captured_at TEXT,"
              " reference_price_usd REAL, reference_liquidity_usd REAL, disposition TEXT, survival_risk REAL,"
              " opportunity_score REAL, assessment_json TEXT)")
    c.execute("CREATE TABLE trench_counterfactuals (candidate_id TEXT, horizon_seconds INTEGER,"
              " final_return_fraction REAL, max_return_fraction REAL, max_drawdown_fraction REAL, observed_at TEXT)")
    c.commit()
    c.close()
    life = str(tmp_path / "l.db")
    al.cycle(db, life, meta_db=meta, rng=random.Random(1))
    r = al.roster(life)
    kinds = [a["kind"] for a in r["agents"]]
    assert kinds.count("meta_launch") == len(al.META_SEEDS)
    assert all(a["status"] == "shadow" and a["weight"] == 0 for a in r["agents"])
