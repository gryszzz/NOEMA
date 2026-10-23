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
