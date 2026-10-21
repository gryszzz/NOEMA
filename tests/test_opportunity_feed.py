import json
import sqlite3
from datetime import UTC, datetime, timedelta

from noema import opportunity_feed as of


def _db(tmp_path):
    p = tmp_path / "noema.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE alpha_candidates (chain, address, symbol, evaluated_at, passed, score, cap_class, price,"
              " liquidity, volume_24h, mcap, age_hours, h1, h6, h24, buzz_velocity, liquidity_growth, screens_json)")
    c.commit()
    return p, c


def _alpha(c, addr, at, price, gate=True):
    c.execute("INSERT INTO alpha_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("solana", addr, addr.upper(), at.isoformat(), 1, 0.5, "meme", price, 1e5, 2e5, 1e6, 30, 1, 2, 3, 0, 0,
               json.dumps({"catalyst_gate": {"pass": gate}})))
    c.commit()


def test_empty_sources_are_reported_not_invented(tmp_path):
    p, _ = _db(tmp_path)
    out = of.refresh(str(p), feed_path=str(tmp_path / "f.db"))
    assert out["items"] == []
    assert all(s["status"] == "empty" for s in out["sources"].values())
    assert out["proven"] is False


def test_dedupe_new_then_expire_with_move(tmp_path):
    p, c = _db(tmp_path)
    t0 = datetime.now(UTC)
    _alpha(c, "abc", t0, 1.0)
    _alpha(c, "def", t0, 2.0, gate=False)
    f = str(tmp_path / "f.db")
    a = of.refresh(str(p), now=t0, feed_path=f)
    assert {i["id"] for i in a["items"]} == {"tok:solana:abc", "tok:solana:def"}
    assert all(i["is_new"] for i in a["items"])
    assert a["items"][0]["id"] == "tok:solana:abc"  # catalyst-gate pass ranks above watch
    assert a["items"][1]["label"] == "WATCH"
    # same rows again later: no duplicates, first_seen kept, no longer NEW after 20 min
    b = of.refresh(str(p), now=t0 + timedelta(minutes=20), feed_path=f)
    assert len(b["items"]) == 2 and not any(i["is_new"] for i in b["items"])
    assert b["items"][0]["first_seen"] == t0.isoformat()
    # abc re-evaluated at a higher price; source window passes for def -> def expires
    c.execute("DELETE FROM alpha_candidates WHERE address='def'")
    _alpha(c, "abc", t0 + timedelta(minutes=25), 1.5)
    d = of.refresh(str(p), now=t0 + timedelta(minutes=30), feed_path=f)
    live = {i["id"]: i for i in d["items"]}
    assert set(live) == {"tok:solana:abc"}
    assert live["tok:solana:abc"]["move_since_found"] == 0.5
    assert [h["id"] for h in d["history"]] == ["tok:solana:def"]
    assert d["counts"]["history"] == 1 and d["measured"]["n"] == 2


def test_history_ages_out(tmp_path):
    p, c = _db(tmp_path)
    t0 = datetime.now(UTC) - timedelta(hours=30)
    _alpha(c, "old", t0, 1.0)
    f = str(tmp_path / "f.db")
    of.refresh(str(p), now=t0, feed_path=f)
    c.execute("DELETE FROM alpha_candidates")
    c.commit()
    of.refresh(str(p), now=t0 + timedelta(hours=1), feed_path=f)
    out = of.refresh(str(p), now=t0 + timedelta(hours=27), feed_path=f)
    assert out["history"] == []
