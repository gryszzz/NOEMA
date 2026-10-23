import json
import os
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from noema import dashboard_app
from noema import prediction_inbox as pi

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)


def _post(**kw):
    doc = {"schema": pi.SCHEMA_ID, "id": "pb-1", "posted_at": "2026-10-22T22:10:00-04:00",
           "author": "Prediction Bot", "title": "Scan", "summary": "two picks",
           "picks": [{"venue": "kalshi", "market": "TX gov: Hinojosa", "market_id": "GOVPARTYTX-26-D",
                      "side": "NO", "price": 0.67, "fair": 0.70, "confidence": "low", "resolves": "2026-11-03"},
                     {"venue": "polymarket", "market": "<img src=x onerror=alert(1)>", "side": "YES",
                      "price": 0.381, "fair": 0.42, "confidence": "medium",
                      "url": "https://polymarket.com/event/taipei"}]}
    doc.update(kw)
    return doc


def _main_db(tmp_path):
    p = tmp_path / "noema.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE market_snapshots (id INTEGER PRIMARY KEY, venue TEXT, market_id TEXT, captured_at TEXT, "
              "valid INT, validation_json TEXT, snapshot_json TEXT)")
    c.execute("CREATE TABLE fs_markets (ticker TEXT PRIMARY KEY, result TEXT)")
    c.execute("CREATE TABLE fs_quotes (ticker TEXT, at TEXT, bid REAL, ask REAL, last REAL)")
    c.execute("INSERT INTO market_snapshots (venue, market_id, captured_at, snapshot_json) VALUES (?,?,?,?)",
              ("kalshi:production", "GOVPARTYTX-26-D", "2026-10-23 02:50:00+00:00",
               json.dumps({"yes_bid": 0.30, "yes_ask": 0.32})))
    c.commit()
    c.close()
    return str(p)


def _drop(inbox, name, doc):
    inbox.mkdir(exist_ok=True)
    (inbox / name).write_text(json.dumps(doc) if not isinstance(doc, str) else doc)


def test_valid_post_is_ingested_marked_and_archived(tmp_path):
    inbox, db, main = tmp_path / "inbox", str(tmp_path / "pb.db"), _main_db(tmp_path)
    _drop(inbox, "a.json", _post())
    s = pi.ingest(db, inbox, main_db=main, now=NOW)
    assert s["accepted"] == 1 and s["rejected"] == 0 and s["marked"] == 1
    assert not (inbox / "a.json").exists() and (inbox / "archive" / "20261023" / "a.json").exists()
    v = pi.room_view(db)
    assert v["paper"] is True and v["status"] == "active"
    k, p = v["posts"][0]["picks"]
    assert k["current"] == pytest.approx(0.69) and k["pnl"] == pytest.approx(0.02)  # NO = 1 - yes mid 0.31
    assert p["current"] is None and p["market"] == "<img src=x onerror=alert(1)>"  # stored as plain text
    assert v["stats"]["open"] == 2 and v["stats"]["hit_rate"] is None
    # same id again -> duplicate, not re-inserted
    _drop(inbox, "b.json", _post())
    assert pi.ingest(db, inbox, main_db=main, now=NOW)["duplicates"] == 1


def test_settles_on_kalshi_resolution_and_hit_rate(tmp_path):
    inbox, db, main = tmp_path / "inbox", str(tmp_path / "pb.db"), _main_db(tmp_path)
    _drop(inbox, "a.json", _post())
    pi.ingest(db, inbox, main_db=main, now=NOW)
    c = sqlite3.connect(main)
    c.execute("INSERT INTO fs_markets VALUES ('GOVPARTYTX-26-D', 'no')")
    c.commit()
    c.close()
    assert pi.ingest(db, inbox, main_db=main, now=NOW)["settled"] == 1
    st = pi.room_view(db)["stats"]
    assert st["settled"] == 1 and st["won"] == 1 and st["hit_rate"] == 1.0
    assert st["paper_pnl_per_contract"] == pytest.approx(0.33)


def test_stale_marks_are_ignored(tmp_path):
    main = _main_db(tmp_path)
    c = pi._ro(main)
    later = datetime(2026, 10, 23, 12, 0, tzinfo=UTC).timestamp()
    assert pi.lookup_mark(c, "kalshi", "GOVPARTYTX-26-D", "YES", later) == (None, None, None)
    c.close()


@pytest.mark.parametrize("bad", [
    {"schema": "tev.prediction_post.v2"},
    {"author": "Someone Else"},
    {"id": "../../etc"},
    {"posted_at": "2026-10-22T22:10:00"},  # no offset
    {"posted_at": "2027-01-01T00:00:00+00:00"},  # future
    {"extra": 1},
    {"picks": []},
    {"title": "x" * 161},
    {"picks": [{"venue": "binance", "market": "m", "side": "YES", "price": 0.5, "fair": 0.6, "confidence": "low"}]},
    {"picks": [{"venue": "kalshi", "market": "m", "side": "BUY", "price": 0.5, "fair": 0.6, "confidence": "low"}]},
    {"picks": [{"venue": "kalshi", "market": "m", "side": "YES", "price": 1.5, "fair": 0.6, "confidence": "low"}]},
    {"picks": [{"venue": "kalshi", "market": "m", "side": "YES", "price": True, "fair": 0.6, "confidence": "low"}]},
    {"picks": [{"venue": "kalshi", "market": "m", "side": "YES", "price": 0.5, "fair": 0.6, "confidence": "low",
                "url": "javascript:alert(1)"}]},
    {"picks": [{"venue": "kalshi", "market": "m", "side": "YES", "price": 0.5, "fair": 0.6, "confidence": "low",
                "url": "https://evil.example/kalshi.com"}]},
    {"picks": [{"venue": "kalshi", "market": "m", "side": "YES", "price": 0.5, "fair": 0.6, "confidence": "low",
                "order": "buy"}]},
])
def test_malformed_posts_are_rejected(bad):
    with pytest.raises(pi.Reject):
        pi.validate_post(_post(**bad), NOW)


def test_oversized_symlink_and_garbage_files_are_rejected(tmp_path):
    inbox, db = tmp_path / "inbox", str(tmp_path / "pb.db")
    _drop(inbox, "big.json", _post(summary="y" * 70_000))
    _drop(inbox, "junk.json", "{not json")
    (tmp_path / "secret.json").write_text(json.dumps(_post(id="pb-sym")))
    os.symlink(tmp_path / "secret.json", inbox / "link.json")
    s = pi.ingest(db, inbox, main_db="", now=NOW)
    assert s["accepted"] == 0 and s["rejected"] == 3
    assert "over 64KB" in s["reasons"]["big.json"] and "regular file" in s["reasons"]["link.json"]
    assert (inbox / "rejected" / "20261023").is_dir()


def test_init_inbox_writes_readme(tmp_path):
    d = pi.init_inbox(tmp_path / "pi")
    assert pi.SCHEMA_ID in (d / "README.md").read_text()


def test_api_and_room_assets(tmp_path, monkeypatch):
    inbox, main = tmp_path / "inbox", _main_db(tmp_path)
    _drop(inbox, "a.json", _post())
    pi.ingest(pi.default_db(main), inbox, main_db=main, now=NOW)
    monkeypatch.delenv("NOEMA_PREDICTION_DB", raising=False)
    monkeypatch.setattr(dashboard_app, "_db_path", lambda: main)
    body = TestClient(dashboard_app.app).get("/api/prediction-bot").json()
    assert body["paper"] is True and len(body["posts"][0]["picks"]) == 2
    html = TestClient(dashboard_app.app).get("/trading-room").text
    assert "/static/prediction-panel.js" in html
    js = (dashboard_app._STATIC_DIR / "prediction-panel.js").read_text()
    assert "innerHTML" not in js.replace("no innerHTML", "") and "purple" not in js


def test_panel_hides_post_text_and_stake_from_note():
    js = (dashboard_app._STATIC_DIR / "prediction-panel.js").read_text()
    for gone in ("p.title", "p.summary", "p.note", "pb-post", "report_path"):
        assert gone not in js
    assert pi.note_stake("stake $4.78, cap $184") == 4.78 and pi.note_stake("stake $1,250.") == 1250.0
    assert pi.note_stake("no stake here") is None and pi.note_stake(None) is None


# --- kind / stake_usd / capacity_usd / parlay legs (all optional, backward compatible) -------------------------

def _parlay(**kw):
    d = {"kind": "parlay", "market": "TX NO + Maine GOP", "price": 0.22, "fair": 0.26, "confidence": "low",
         "stake_usd": 5, "capacity_usd": 40,
         "legs": [{"venue": "kalshi", "market": "TX gov", "market_id": "GOVPARTYTX-26-D", "side": "NO", "price": 0.67},
                  {"venue": "polymarket", "market": "Maine GOP", "market_id": "maine-gop", "side": "YES",
                   "price": 0.33}]}
    d.update(kw)
    return d


def test_v1_picks_without_new_fields_still_valid():
    v = pi.validate_post(_post(), NOW)
    assert all(p["kind"] == "single" and p["legs"] is None for p in v["picks"])


@pytest.mark.parametrize("pick", [
    {"capacity_usd": 0}, {"capacity_usd": -1}, {"capacity_usd": "10"}, {"capacity_usd": float("inf")},
    {"stake_usd": 0}, {"stake_usd": True}, {"kind": "combo"},
    {"legs": _parlay()["legs"]},                                    # legs on a single
    {"kind": "scaled", "legs": _parlay()["legs"]},                  # legs on scaled
])
def test_bad_new_pick_fields_rejected(pick):
    base = dict(_post()["picks"][0])
    base.update(pick)
    with pytest.raises(pi.Reject):
        pi.validate_post(_post(picks=[base]), NOW)


@pytest.mark.parametrize("legs", [None, [], _parlay()["legs"][:1], _parlay()["legs"] * 6,
                                  [{**_parlay()["legs"][0], "price": 1.0}, _parlay()["legs"][1]],
                                  [{**_parlay()["legs"][0], "evil": 1}, _parlay()["legs"][1]],
                                  [{**_parlay()["legs"][0], "venue": "binance"}, _parlay()["legs"][1]]])
def test_bad_parlay_legs_rejected(legs):
    p = _parlay(legs=legs)
    if legs is None:
        p.pop("legs")
    with pytest.raises(pi.Reject):
        pi.validate_post(_post(picks=[p]), NOW)


def test_parlay_and_capacity_fields_accepted():
    v = pi.validate_post(_post(picks=[_parlay(), {**_post()["picks"][0], "capacity_usd": 250, "stake_usd": 25}]), NOW)
    par, single = v["picks"]
    assert par["kind"] == "parlay" and len(par["legs"]) == 2 and par["side"] == "YES"
    assert single["capacity_usd"] == 250 and single["stake_usd"] == 25


class _FakeQuotes:
    def __init__(self, table):
        self.table = table

    def quote(self, venue, mid, text=""):
        from noema.public_quotes import Quote
        px, res = self.table.get(mid, (None, None))
        if px is None and res is None:
            return None
        return Quote(venue, mid, None, None, px, res, "active", text, 0, "fake")


def _ingest_parlay(tmp_path, table):
    inbox, db, main = tmp_path / "inbox", str(tmp_path / "pb.db"), _main_db(tmp_path)
    _drop(inbox, "a.json", _post(picks=[_parlay()]))
    pi.ingest(db, inbox, main_db=main, now=NOW, quotes=_FakeQuotes(table))
    return db, main, inbox


def test_parlay_marked_leg_by_leg(tmp_path):
    db, _, _ = _ingest_parlay(tmp_path, {"maine-gop": (0.40, None)})
    pk = pi.room_view(db)["posts"][0]["picks"][0]
    # TX NO leg marked by NOEMA's own quote (yes mid .31 -> NO .69), Maine YES .40
    assert pk["kind"] == "parlay" and pk["status"] == "open"
    assert pk["current"] == pytest.approx(round(0.69 * 0.40, 4))
    assert [x["current"] for x in pk["legs"]] == [pytest.approx(0.69), pytest.approx(0.40)]
    assert pk["posted_stake_usd"] == 5 and pk["capacity_usd"] == 40


def test_parlay_settles_only_when_all_legs_resolve(tmp_path):
    db, main, inbox = _ingest_parlay(tmp_path, {"maine-gop": (None, "yes")})
    assert pi.room_view(db)["posts"][0]["picks"][0]["status"] == "open"  # TX leg unresolved
    c = sqlite3.connect(main)
    c.execute("INSERT INTO fs_markets VALUES ('GOVPARTYTX-26-D', 'no')")
    c.commit()
    c.close()
    pi.ingest(db, inbox, main_db=main, now=NOW, quotes=_FakeQuotes({"maine-gop": (None, "yes")}))
    pk = pi.room_view(db)["posts"][0]["picks"][0]
    assert pk["status"] == "settled" and pk["outcome"] == "won" and pk["pnl"] == pytest.approx(0.78)


def test_any_losing_leg_loses_parlay_immediately(tmp_path):
    db, _, _ = _ingest_parlay(tmp_path, {"maine-gop": (None, "no")})
    pk = pi.room_view(db)["posts"][0]["picks"][0]
    assert pk["status"] == "settled" and pk["outcome"] == "lost" and pk["pnl"] == pytest.approx(-0.22)


def test_scaled_pick_is_planned_not_open(tmp_path):
    inbox, db, main = tmp_path / "inbox", str(tmp_path / "pb.db"), _main_db(tmp_path)
    scaled = {**_post()["picks"][0], "kind": "scaled", "stake_usd": 10, "capacity_usd": 120}
    _drop(inbox, "a.json", _post(picks=[scaled]))
    c = sqlite3.connect(main)
    c.execute("INSERT INTO fs_markets VALUES ('GOVPARTYTX-26-D', 'no')")
    c.commit()
    c.close()
    pi.ingest(db, inbox, main_db=main, now=NOW)
    v = pi.room_view(db)
    pk = v["posts"][0]["picks"][0]
    assert pk["status"] == "planned" and pk["pnl"] is None and pk["kind"] == "scaled"
    assert v["stats"]["open"] == 0 and v["stats"]["planned"] == 1 and v["stats"]["settled"] == 0


def test_readme_documents_new_fields_and_parlay_example(tmp_path):
    pi.init_inbox(tmp_path / "inbox")
    txt = (tmp_path / "inbox" / "README.md").read_text()
    for k in ("`kind`", "`stake_usd`", "`legs`", "\"kind\": \"parlay\"", "planned"):
        assert k in txt
    body = txt.split("```json", 1)[1].split("```", 1)[0]
    assert pi.validate_post(json.loads(body), datetime(2026, 10, 23, 3, 0, tzinfo=UTC))


def _pair(**kw):
    d = {"kind": "pair", "resolves": "2026-11-03", "stake_usd": 20, "rules_match": True,
         "legs": [{"venue": "kalshi", "market": "TX gov", "market_id": "GOVPARTYTX-26-D", "side": "NO", "price": 0.67,
                   "url": "https://kalshi.com/markets/govpartytx"},
                  {"venue": "polymarket", "market": "TX gov Dem", "market_id": "tx-dem", "side": "YES",
                   "price": 0.30}]}
    d.update(kw)
    return d


def test_pair_validation():
    v = pi.validate_post(_post(picks=[_pair()]), NOW)["picks"][0]
    assert v["kind"] == "pair" and v["price"] == pytest.approx(0.97) and v["fair"] == 1.0 and v["rules_match"] is True
    assert v["market"] == "TX gov" and v["legs"][0]["url"].startswith("https://kalshi.com")
    assert pi.validate_post(_post(picks=[_pair(rules_match=None)]), NOW)["picks"][0]["rules_match"] is None
    legs = _pair()["legs"]
    for bad in (_pair(legs=legs[:1]), _pair(legs=legs + [legs[0]]),
                _pair(legs=[legs[0], {**legs[1], "venue": "kalshi"}]),       # same venue
                _pair(legs=[legs[0], {**legs[1], "side": "NO"}]),            # same outcome
                _pair(rules_match="yes"), _pair(price=0.80), _pair(resolves=None),
                _pair(legs=[legs[0], {**legs[1], "url": "https://evil.example"}]),
                {**_post()["picks"][0], "rules_match": True}):               # rules_match on a single
        with pytest.raises(pi.Reject):
            pi.validate_post(_post(picks=[bad]), NOW)


def test_pair_metrics_and_unverified():
    m = pi.pair_metrics(_pair()["legs"], "2026-11-03", 1, today=NOW.date())
    from noema.paper_risk import fee
    cost = 0.97 + fee("kalshi", 0.67) + fee("polymarket", 0.30)
    assert m["combined_cost"] == pytest.approx(round(cost, 4))
    assert m["profit_per_usd"] == pytest.approx(round((1 - cost) / cost, 4), abs=1e-4)
    assert m["days_to_resolve"] == 11 and m["rules_match"] is True
    u = pi.pair_metrics([{**x, "price": x["price"] - 0.05} for x in _pair()["legs"]], "2026-11-03", None,
                        today=NOW.date())
    assert u["verification"] == "unverified" and u["locked"] is False and u["profit_per_usd"] > 0


def test_pair_settles_when_both_legs_resolve(tmp_path):
    inbox, db, main = tmp_path / "inbox", str(tmp_path / "pb.db"), _main_db(tmp_path)
    _drop(inbox, "a.json", _post(picks=[_pair()]))
    q = _FakeQuotes({"tx-dem": (0.31, None)})
    pi.ingest(db, inbox, main_db=main, now=NOW, quotes=q)
    pk = pi.room_view(db)["picks"][0]
    assert pk["status"] == "open" and pk["current"] == pytest.approx(0.69 + 0.31) and pk["pair"]["rules_match"]
    assert pk["pair"]["locked"] is False  # 0.97 + fees >= $1: nothing to lock
    c = sqlite3.connect(main)
    c.execute("INSERT INTO fs_markets VALUES ('GOVPARTYTX-26-D', 'no')")
    c.commit()
    c.close()
    pi.ingest(db, inbox, main_db=main, now=NOW, quotes=_FakeQuotes({"tx-dem": (None, "no")}))
    pk = pi.room_view(db)["picks"][0]
    assert pk["status"] == "settled" and pk["pnl"] == pytest.approx(0.03)


def test_correction_shows_only_latest_version(tmp_path):
    inbox, db, main = tmp_path / "inbox", str(tmp_path / "pb.db"), _main_db(tmp_path)
    _drop(inbox, "a.json", _post())
    pi.ingest(db, inbox, main_db=main, now=NOW)
    fixed = {**_post()["picks"][0], "price": 0.66, "fair": 0.72, "market": "TX gov (fixed)"}
    _drop(inbox, "b.json", _post(id="pb-2", posted_at="2026-10-22T22:40:00-04:00", title="Correction: x",
                                 picks=[fixed]))
    pi.ingest(db, inbox, main_db=main, now=NOW)
    v = pi.room_view(db)
    tx = [p for p in v["picks"] if p["market_id"] == "GOVPARTYTX-26-D"]
    assert len(tx) == 1 and tx[0]["price"] == 0.66 and tx[0]["post_id"] == "pb-2"
    assert len(v["picks"]) == 2 and v["stats"]["superseded_or_hidden"] == 1
    assert "Correction" not in json.dumps(v["picks"])


def test_hidden_picks_not_displayed(tmp_path):
    inbox, db, main = tmp_path / "inbox", str(tmp_path / "pb.db"), _main_db(tmp_path)
    _drop(inbox, "a.json", _post())
    pi.ingest(db, inbox, main_db=main, now=NOW)
    c = sqlite3.connect(db)
    c.execute("UPDATE prediction_picks SET hidden=1 WHERE market_id='GOVPARTYTX-26-D'")
    c.commit()
    c.close()
    assert [p["market_id"] for p in pi.room_view(db)["picks"]] == [None]


def test_panel_has_no_post_text_or_banned_words():
    from pathlib import Path
    js = (Path(pi.__file__).parent / "static" / "prediction-panel.js").read_text()
    for w in ("summary", ".title", ".note", "innerHTML", "arb", "seed", "correction"):
        assert w not in js.lower() if w != ".title" else "p.title" not in js
    for g in ("Singles", "Pairs", "Parlays", "Scaled"):
        assert g in js
    readme = pi.README.lower()
    assert " arb" not in readme and "seed" not in readme and '"kind": "pair"' in readme


def test_voids_close_picks_without_deleting(tmp_path):
    inbox, main = tmp_path / "inbox", _main_db(tmp_path)
    _drop(inbox, "a.json", _post())
    db = pi.default_db(main)
    pi.ingest(db, inbox, main_db=main, now=NOW)
    before = pi.room_view(db)["stats"]
    void = {"schema": pi.SCHEMA_ID, "id": "pb-void-1", "author": pi.AUTHOR, "title": "void", "summary": "s",
            "posted_at": NOW.isoformat(), "voids": [{"post_id": _post()["id"], "idx": 0, "reason": "withdrawn"},
                                                    {"post_id": "pb-nope"}]}
    _drop(inbox, "b.json", void)
    st = pi.ingest(db, inbox, main_db=main, now=NOW)
    assert st["accepted"] == 1 and st["voided"] == 1 and "b.json" in st["reasons"]
    after = pi.room_view(db)["stats"]
    assert after["open"] == before["open"] - 1 and after["voided"] == 1
    import sqlite3
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*), SUM(status='void') FROM prediction_picks").fetchone() == (2, 1)
    for bad in ([{"post_id": "pb-void-1"}], [{"post_id": "x", "idx": -1}], [{"post_id": "x", "pick": 1}], []):
        with pytest.raises(pi.Reject):
            pi.validate_post({**void, "voids": bad}, NOW)
    readme_void = next(b.split("```")[0] for b in pi.README.split("```json") if '"voids"' in b.split("```")[0])
    import json
    assert pi.validate_post(json.loads(readme_void), NOW)["voids"]
