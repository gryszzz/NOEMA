from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from noema import pm_watch

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)


def _main_db(tmp_path) -> str:
    path = str(tmp_path / "noema.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE x_market_links (venue TEXT, market_id TEXT, linked_at TEXT);
        CREATE TABLE news_market_links (venue TEXT, market_id TEXT, linked_at TEXT);
        CREATE TABLE xv_pairs (pm_market TEXT, at TEXT);
        CREATE TABLE forecast_ledger (id INTEGER PRIMARY KEY, venue TEXT, market_id TEXT);
    """)
    c.execute("INSERT INTO x_market_links VALUES ('polymarket-us','a',?)", ((NOW - timedelta(hours=1)).isoformat(),))
    c.execute("INSERT INTO x_market_links VALUES ('polymarket-us','old',?)", ((NOW - timedelta(hours=9)).isoformat(),))
    c.execute("INSERT INTO x_market_links VALUES ('kalshi','K1',?)", (NOW.isoformat(),))
    c.execute("INSERT INTO news_market_links VALUES ('polymarket-us','b',?)", (NOW.isoformat(),))
    c.execute("INSERT INTO xv_pairs VALUES ('a', ?)", (NOW.isoformat(),))
    c.execute("INSERT INTO xv_pairs VALUES ('c', ?)", (NOW.isoformat(),))
    c.execute("INSERT INTO forecast_ledger (venue, market_id) VALUES ('polymarket-us','d')")
    c.commit()
    c.close()
    return path


def test_watched_priority_dedup_and_cap(tmp_path) -> None:
    main = _main_db(tmp_path)
    assert pm_watch.watched(main, NOW) == [("a", "links"), ("b", "links"), ("c", "pairs"), ("d", "recent")]
    assert pm_watch.watched(main, NOW, cap=2) == [("a", "links"), ("b", "links")]


def test_quotes_from_markets_parses_list_quotes_and_drops_crossed() -> None:
    q = pm_watch.quotes_from_markets([
        {"slug": "a", "bestBidQuote": {"value": "0.40"}, "bestAskQuote": {"value": "0.42"}},
        {"slug": "x", "bestBidQuote": {"value": "0.60"}, "bestAskQuote": {"value": "0.50"}},
        {"slug": "z", "closed": True, "bestBidQuote": {"value": "0.1"}},
    ])
    assert q == {"a": (0.40, 0.42), "x": (None, None)}


def test_run_once_stores_and_readers_respect_age(tmp_path) -> None:
    main, db = _main_db(tmp_path), str(tmp_path / "pm_watch.db")
    calls: list[list[str]] = []

    def fetch(slugs):
        calls.append(slugs)
        return {"a": (0.4, 0.42), "b": (None, 0.7), "c": (None, None)}

    s = pm_watch.run_once(main, db, now=NOW, fetch=fetch)
    assert calls == [["a", "b", "c", "d"]]
    assert s["watched"] == 4 and s["quoted"] == 2 and s["by_reason"] == {"links": 2}
    got = pm_watch.latest_many(["a", "b", "c"], max_age_s=45, now=NOW + timedelta(seconds=30), db=db)
    assert set(got) == {"a", "b"} and got["a"][:2] == (0.4, 0.42)
    assert pm_watch.latest_many(["a"], max_age_s=45, now=NOW + timedelta(seconds=60), db=db) == {}
    assert pm_watch.quote_at("a", NOW + timedelta(seconds=60), db=db) == (0.4, 0.42)
    assert pm_watch.quote_at("a", NOW - timedelta(seconds=1), db=db) is None


def test_run_once_survives_fetch_error_and_missing_db(tmp_path) -> None:
    main, db = _main_db(tmp_path), str(tmp_path / "pm_watch.db")

    def boom(_slugs):
        raise RuntimeError("rate limited")

    s = pm_watch.run_once(main, db, now=NOW, fetch=boom)
    assert s["error"] == "RuntimeError" and s["quoted"] == 0
    assert pm_watch.latest_many(["a"], db=str(tmp_path / "missing.db")) == {}
    assert pm_watch.quote_at("a", NOW, db=str(tmp_path / "missing.db")) is None
