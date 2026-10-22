import json
import sqlite3

from fastapi.testclient import TestClient

from noema import room_v7
from noema.room_v7 import build_room_v7, stripe_panel


def _db(tmp_path, name="n.db"):
    p = tmp_path / name
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE trench_desk_events (id INTEGER PRIMARY KEY, ts TEXT, desk_id TEXT, event TEXT, "
              "payload_json TEXT)")
    cycle = {"pools_seen": 10, "prefilter_passed": 4, "holder_checked": 2, "entry_passed": 1, "entries": 0,
             "pumpfun_new_launches": 5, "rejections": {"liquidity below desk floor": 6},
             "chief": {"min_probability": 0.6, "min_confidence": 0.55},
             "feed": [{"symbol": "AAA", "stage": "rejected", "reason": "liquidity below desk floor",
                       "price_change_m5": 2.0, "candles": [[1, 1, 1, 1, 1.0, 5], [2, 1, 1, 1, 1.2, 5]]},
                      {"symbol": "BBB", "stage": "filters", "price_change_m5": 1.0, "candles": []}]}
    c.executemany("INSERT INTO trench_desk_events (ts, desk_id, event, payload_json) VALUES (?,?,?,?)", [
        ("2026-10-21T20:00:00+00:00", None, "desk_cycle", json.dumps(cycle)),
        ("2026-10-21T20:00:01+00:00", None, "chief_decision",
         json.dumps({"approved": False, "symbol": "AAA", "probability": 0.4, "confidence": 0.8, "reason": "p<0.6"})),
        ("2026-10-21T20:00:02+00:00", None, "equity_snapshot", json.dumps({"equity_usd": 100})),
    ])
    c.execute("CREATE TABLE capital_guard (id INTEGER PRIMARY KEY, peak_equity TEXT, floor TEXT, equity TEXT, "
              "day TEXT, day_start_equity TEXT, tripped INT, trip_reason TEXT, updated_at TEXT)")
    c.execute("INSERT INTO capital_guard VALUES (1,'10','7.5','9','2026-10-21','10',0,NULL,'x')")
    c.commit()
    c.close()
    return str(p)


def test_room_v7_is_built_from_real_rows(tmp_path):
    out = build_room_v7(_db(tmp_path), fetch=False)
    assert out["paper"] is True
    assert out["funnel"]["latest"]["crawled"] == 10 and out["funnel"]["latest"]["judged"] == 1
    assert out["funnel"]["rejects"] == [{"reason": "liquidity below desk floor", "count": 6}]
    assert out["funnel"]["scan"][0]["closes"] == [1.0, 1.2]
    assert [e["event"] for e in out["events"]] == ["chief_decision", "desk_cycle"]  # no equity noise
    assert out["chief"]["last"]["symbol"] == "AAA"
    assert out["equity"]["guard"]["day_pnl_usd"] == -1.0
    assert out["regime"]["n"] == 2
    assert "VETO AAA" in out["headline"]
    assert build_room_v7(_db(tmp_path, "m.db"), since_id=2, fetch=False)["events"] == []


def test_room_v7_missing_db_has_explicit_empty_state(tmp_path):
    out = build_room_v7(str(tmp_path / "none.db"), fetch=False)
    assert out["funnel"]["scan"] == [] and out["regime"]["label"] == "UNKNOWN"


def test_stripe_panel_requires_restricted_key(monkeypatch):
    assert stripe_panel({})["status"] == "not_connected"
    assert stripe_panel({"NOEMA_STRIPE_READ_KEY": "sk_live_x"})["status"] == "refused"
    room_v7._CACHE.pop("stripe", None)
    calls = []

    def fake(key, path, params=None):
        calls.append(path)
        return {"balance": {"livemode": True, "available": [{"amount": 1234, "currency": "usd"}], "pending": []},
                "charges": {"data": [{"created": 1, "amount": 500, "currency": "usd", "status": "succeeded"}]},
                "payouts": {"data": []}}[path]

    monkeypatch.setattr(room_v7, "_stripe_get", fake)
    out = stripe_panel({"NOEMA_STRIPE_READ_KEY": "rk_live_test"})
    assert out["status"] == "connected" and out["available"][0]["amount_minor"] == 1234
    assert "rk_live_test" not in json.dumps(out)
    assert calls == ["balance", "charges", "payouts"]
    room_v7._CACHE.pop("stripe", None)


def test_room_v7_route_and_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_DB_PATH", _db(tmp_path))
    monkeypatch.setattr(room_v7, "prices", dict)
    from noema.dashboard_app import app
    client = TestClient(app)
    r = client.get("/api/room-v7?since=0")
    assert r.status_code == 200 and r.json()["funnel"]["latest"]["crawled"] == 10
    assert client.get("/static/room-v7.js").status_code == 200
    assert "/static/room-v7.js" in client.get("/trading-room").text


def test_v7_grid_accounts_for_every_v6_row():
    from pathlib import Path
    css = (Path(__file__).parents[1] / "noema" / "static" / "room-v7.css").read_text()
    html = (Path(__file__).parents[1] / "noema" / "static" / "trading-room-v6.html").read_text()
    if "culture-row" in html:
        assert ".culture-row{grid-row:6!important}" in css and ".left{grid-row:4/7!important}" in css


def test_onepage_assets_are_included(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_DB_PATH", _db(tmp_path, "d.db"))
    from noema.dashboard_app import app
    client = TestClient(app)
    html = client.get("/trading-room").text
    for asset in ("/static/room-v7-onepage.js", "/static/room-v7-onepage.css"):
        assert asset in html and client.get(asset).status_code == 200


def _crawl_db(tmp_path):
    p = _db(tmp_path, "c.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE news_fetch_state (url TEXT, status TEXT, etag TEXT, last_modified TEXT, fetched_at TEXT)")
    c.execute("INSERT INTO news_fetch_state VALUES ('https://feeds.bbci.co.uk/news/rss.xml','200',NULL,NULL,"
              "'2026-10-21T20:00:05+00:00')")
    c.execute("CREATE TABLE culture_source_state (name TEXT, domain TEXT, url TEXT, status TEXT, items INT, fetched_at TEXT)")
    c.execute("INSERT INTO culture_source_state VALUES ('pumpfun_new','crypto','https://frontend-api.pump.fun/x','200',20,"
              "'2026-10-21T20:00:04+00:00')")
    c.execute("CREATE TABLE x_ingest_runs (id INTEGER PRIMARY KEY, at TEXT, posts_seen INT, posts_new INT, linked INT)")
    c.execute("INSERT INTO x_ingest_runs (at, posts_seen, posts_new, linked) VALUES ('2026-10-21T20:00:03+00:00',9,7,2)")
    c.execute("CREATE TABLE market_scan_state (source TEXT, cursor TEXT, updated_at TEXT)")
    c.execute("INSERT INTO market_scan_state VALUES ('kalshi:production:markets','c','2026-10-21T20:00:02+00:00')")
    c.execute("CREATE TABLE news_feed_schedule (name TEXT, next_due TEXT, failures INT, last_status TEXT, "
              "last_fetch_at TEXT, last_new INT)")
    c.execute("INSERT INTO news_feed_schedule VALUES ('BBC','2026-10-21T20:05:00+00:00',0,'200','x',0)")
    c.commit()
    c.close()
    return p


def test_crawl_feed_lists_only_recorded_fetches(tmp_path):
    out = build_room_v7(_crawl_db(tmp_path), fetch=False)["crawl"]
    kinds = [h["kind"] for h in out["hits"]]
    assert kinds[:4] == ["news", "pumpfun", "x", "prediction"]  # newest first
    assert out["hits"][0]["url"] == "https://feeds.bbci.co.uk/news/rss.xml"
    assert out["hits"][2]["items"] == 9 and "2 linked" in out["hits"][2]["detail"]
    assert {h["kind"] for h in out["hits"]} >= {"pool"}  # scanned pools from the latest sweep
    assert out["next"]["news"].startswith("2026-10-21T20:05") and out["next"]["x"].startswith("2026-10-21T20:30")
    assert out["counters"]["sweeps_24h"] >= 0


def test_crawl_feed_empty_without_tables(tmp_path):
    out = build_room_v7(_db(tmp_path, "e.db"), fetch=False)["crawl"]
    assert [h for h in out["hits"] if h["kind"] != "pool"] == [] and out["next"] == {}


def test_room_assets_are_versioned_and_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_DB_PATH", _db(tmp_path, "v.db"))
    from noema.dashboard_app import app, versioned_assets
    client = TestClient(app)
    r = client.get("/trading-room")
    for asset in ("room-v7.js", "room-v7.css", "crawler-hero.js", "crawler-hero.css"):
        assert f"/static/{asset}?v=" in r.text
        assert client.get(f"/static/{asset}").headers["cache-control"].startswith("no-cache")
    assert r.headers["cache-control"].startswith("no-cache")
    assert versioned_assets('<script src="/static/nope.js"></script>') == '<script src="/static/nope.js"></script>'


def test_source_lights_survive_feed_truncation():
    from noema.room_v7 import _latest_by_light, light_of
    hits = [{"at": f"2026-10-21T20:{59 - i:02d}:00+00:00", "kind": "news", "source": "bbc", "url": "u", "status": "200"}
            for i in range(70)]
    hits.append({"at": "2026-10-21T18:00:00+00:00", "kind": "x", "source": "X read", "url": "x-inbox", "status": "ok"})
    hits.append({"at": "2026-10-21T17:00:00+00:00", "kind": "culture", "source": "dexscreener_boosts_top",
                 "url": "https://api.dexscreener.com/token-boosts/top/v1", "status": "200"})
    by = _latest_by_light(hits)
    assert by["x"]["at"].startswith("2026-10-21T18") and "dexscreener" in by
    assert light_of({"kind": "prediction", "source": "kalshi_markets"}) == "kalshi"


def test_scan_items_carry_real_ohlc_and_interval(tmp_path):
    out = build_room_v7(_db(tmp_path), fetch=False)
    a, b = out["funnel"]["scan"][:2]
    assert a["ohlc"] == [[1.0, 1.0, 1.0, 1.0, 1.0, 5.0], [2.0, 1.0, 1.0, 1.0, 1.2, 5.0]]
    assert a["markers"] == [] and b["ohlc"] == [] and b["interval"] is None  # no synthetic candles


def test_pm_candles_aggregate_stored_quotes(tmp_path):
    from datetime import UTC, datetime, timedelta
    p = _db(tmp_path)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE market_snapshots (id INTEGER PRIMARY KEY, venue TEXT, market_id TEXT, captured_at TEXT, "
              "valid INT, validation_json TEXT, snapshot_json TEXT)")
    t0 = datetime.now(UTC) - timedelta(hours=2)
    for i, (b, a) in enumerate([(0.40, 0.44), (0.45, 0.47), (0.38, 0.40), (0.50, 0.52), (0.48, 0.50)]):
        c.execute("INSERT INTO market_snapshots (venue, market_id, captured_at, valid, snapshot_json) VALUES (?,?,?,?,?)",
                  ("kalshi", "KX1", (t0 + timedelta(minutes=20 * i)).isoformat(), 1,
                   json.dumps({"title": "T", "yes_bid": b, "yes_ask": a})))
    c.execute("INSERT INTO market_snapshots (venue, market_id, captured_at, valid, snapshot_json) VALUES (?,?,?,?,?)",
              ("kalshi", "ONE", t0.isoformat(), 1, json.dumps({"yes_bid": 0.1, "yes_ask": 0.2})))
    c.commit()
    c.close()
    pm = build_room_v7(p, fetch=False)["pm_candles"]
    assert [m["market_id"] for m in pm["markets"]] == ["KX1"]  # single-quote market gets no fake candle
    m = pm["markets"][0]
    assert m["interval"] == "15m" and m["quotes"] == 5
    assert sum(k[5] for k in m["ohlc"]) == 5
    assert all(k[3] <= min(k[1], k[4]) and k[2] >= max(k[1], k[4]) for k in m["ohlc"])


def test_pm_candles_empty_state_is_explicit(tmp_path):
    assert "no market_snapshots" in build_room_v7(_db(tmp_path), fetch=False)["pm_candles"]["status"]


def test_self_built_candles_keep_gaps_and_need_three_buckets():
    from noema.room_v7 import _self_built
    assert _self_built([(0, 1.0), (10, 2.0)]) == []
    rows = _self_built([(0, 1.0), (100, 2.0), (310, 1.5), (1300, 1.4)])
    assert [r[0] for r in rows] == [0.0, 300.0, 1200.0]  # no interpolated candle for the empty buckets
    assert rows[0][1:5] == [1.0, 2.0, 1.0, 2.0] and rows[0][5] == 0.0
