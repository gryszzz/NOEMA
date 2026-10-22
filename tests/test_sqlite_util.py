import sqlite3

import pytest

from noema.outcomes import OutcomeStore
from noema.sqlite_util import connect, retry_write


def test_connect_sets_wal_and_busy_timeout(tmp_path):
    c = connect(tmp_path / "x.db")
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000


def test_retry_write_retries_busy_then_succeeds(monkeypatch):
    monkeypatch.setattr("noema.sqlite_util.time.sleep", lambda _s: None)
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert retry_write(fn) == "ok" and len(calls) == 3


def test_retry_write_does_not_swallow_other_errors():
    with pytest.raises(sqlite3.OperationalError):
        retry_write(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("no such table")))


def test_outcome_upsert_waits_out_a_short_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("noema.sqlite_util.time.sleep", lambda _s: None)
    store = OutcomeStore(str(tmp_path / "noema.db"))
    store.upsert(venue="kalshi", market_id="M", outcome_yes=1, resolved_at=None, raw={})
    assert store.conn.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 1


def test_rpc_bucket_spaces_requests(monkeypatch):
    import asyncio

    from noema import solana_research as sr

    slept = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(sr.asyncio, "sleep", fake_sleep)
    b = sr._TokenBucket(rate=2.0)

    async def go():
        for _ in range(3):
            await b.take()

    asyncio.run(go())
    assert b.requests == 3 and len(slept) == 2 and slept[-1] >= 0.9
