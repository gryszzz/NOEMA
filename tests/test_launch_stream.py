import sqlite3

from noema import launch_stream as ls
from noema import meta_launch as ml
from noema import pmus_history as ph


def test_pumpportal_create_and_trade(tmp_path):
    c = ls.connect(str(tmp_path / "s.db"))
    f: dict = {}
    assert ls.handle_pumpportal(c, {"txType": "create", "mint": "M1", "signature": "S1", "name": "Dog Moon",
                                    "symbol": "DMOON", "solAmount": 1.0}, f) == "M1"
    ls.handle_pumpportal(c, {"txType": "buy", "mint": "M1", "signature": "S2", "solAmount": 0.5}, f)
    assert c.execute("SELECT COUNT(*) FROM launch_trades").fetchone()[0] == 1
    c.commit()
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE trench_launches (mint TEXT, first_pool_at TEXT, discovery_json TEXT)")
    got = ml.load_launches(c, "2000-01-01")
    assert got and got[0]["d"]["stream_only"]
    ok, bad, _ = ml.shield(got[0], {}, {})
    assert not ok and "stream-only" in bad[0]


def test_rpc_url_is_free_first(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    assert "helius" not in ls.rpc_ws_url() and ls.helius_ws_url() is None and ls.helius_http_url() is None
    monkeypatch.setenv("HELIUS_API_KEY", "x")
    assert "helius" not in ls.rpc_ws_url()  # a key no longer moves the firehose onto Helius
    assert ls.helius_ws_url().startswith("wss://mainnet.helius-rpc.com/")


def test_pmus_yes_price():
    assert ph.yes_price({"marketSides": [{"long": True, "price": "0.638"}]}) == 0.638


def _ev(name, body):
    import base64
    import hashlib
    return "Program data: " + base64.b64encode(hashlib.sha256(f"event:{name}".encode()).digest()[:8] + body).decode()


def test_rpc_trade_decode_follow_window(tmp_path):
    c = ls.connect(str(tmp_path / "t.db"))
    mint, user = bytes(range(1, 33)), bytes([9]) * 32
    s = lambda x: len(x).to_bytes(4, "little") + x
    create = _ev("CreateEvent", s(b"Dog") + s(b"DOG") + s(b"u") + mint + bytes(32) + user)
    trade = _ev("TradeEvent", mint + (10**9).to_bytes(8, "little") + (5 * 10**6).to_bytes(8, "little") + b"\x01" + user
                + (0).to_bytes(8, "little") + (30 * 10**9).to_bytes(8, "little") + (10**15).to_bytes(8, "little") + bytes(40))
    f: dict = {}
    assert ls.handle_rpc_logs(c, "S0", [trade], f, now=100) == 0  # not followed yet
    assert ls.handle_rpc_logs(c, "S1", ["Program log: x", create, trade, trade], f, now=100) == 2
    r = c.execute("SELECT mint, side, sol, tokens, trader, market_cap_sol FROM launch_trades WHERE signature='S1'").fetchone()
    assert r == (ls.b58(mint), "buy", 1.0, 5.0, ls.b58(user), 30.0)
    assert ls.handle_rpc_logs(c, "S2", [trade], f, now=100 + ls.TRADE_FOLLOW_S + 1) == 0  # window over
    ls.prune_follow(f, now=100 + ls.TRADE_FOLLOW_S + 1)
    assert not f


def test_b58_known():
    assert ls.b58(bytes(32)) == "1" * 32
    assert ls.b58(bytes([0, 0, 1])) == "112"


def test_launchlab_decode_and_dedupe(tmp_path):
    import base64
    import hashlib
    c = ls.connect(str(tmp_path / "l.db"))
    pool = bytes([7]) * 32
    d = lambda n: hashlib.sha256(f"event:{n}".encode()).digest()[:8]
    u = lambda *xs: b"".join(x.to_bytes(8, "little") for x in xs)
    create = "Program data: " + base64.b64encode(d("PoolCreateEvent") + pool + bytes(64)).decode()
    tr = d("TradeEvent") + pool + u(0, 10**15, 30 * 10**9, 0, 0, 5 * 10**6, 10**9) + bytes(48) + b"\x00\x00\x01"
    assert len(tr) == 147
    trade = "Program data: " + base64.b64encode(tr).decode()
    pools: dict = {}
    n, new = ls.handle_launchlab_logs(c, "C1", [create], pools, now=10)
    assert new == [ls.b58(pool)] and n == 0
    assert ls.handle_launchlab_logs(c, "T1", [trade], pools, now=11)[0] == 0  # mint not resolved yet
    pools[ls.b58(pool)]["mint"] = "MINTx"
    assert ls.handle_launchlab_logs(c, "T2", [trade], pools, now=12)[0] == 1
    r = c.execute("SELECT mint, side, sol, tokens, source FROM launch_trades WHERE signature='T2'").fetchone()
    assert r == ("MINTx", "buy", 1.0, 5.0, "helius_launchlab")
    # PumpPortal + Helius same signature -> one trade row, both arrivals recorded
    ls.insert_trade(c, "S9", "M", "buy", 1, 1, "u", 1, "helius")
    assert ls.insert_trade(c, "S9", "M", "buy", 1, 1, "u", 1, "pumpportal") == 0
    assert c.execute("SELECT COUNT(*) FROM launch_trade_seen WHERE signature='S9'").fetchone()[0] == 2


def test_mint_from_tx():
    tx = {"meta": {"postTokenBalances": [{"mint": ls.WSOL}, {"mint": "BASE"}]}}
    assert ls.mint_from_tx(tx) == ("BASE", True)


def test_pumpportal_url(monkeypatch):
    monkeypatch.delenv("PUMPPORTAL_API_KEY", raising=False)
    assert ls.pumpportal_url() == ls.PUMPPORTAL
    monkeypatch.setenv("PUMPPORTAL_API_KEY", "k")
    assert ls.pumpportal_url().startswith(ls.PUMPPORTAL + "?api-key=")


def test_prune_uses_shared_connection_while_txn_open(tmp_path):
    """Regression: prune used to open a 2nd connection while the stream's shared one held a write txn ->
    'database is locked' crash loop. It must run on the same connection, in batches."""
    import asyncio
    db = str(tmp_path / "s.db")
    c = ls.connect(db)
    c.execute("PRAGMA busy_timeout=200")
    for i in range(12):
        ls.insert_trade(c, f"OLD{i}", "M", "buy", 1.0, 1.0, "T", 1.0, "helius")
    c.execute("UPDATE launch_trades SET received_at='2000-01-01T00:00:00+00:00'")
    c.execute("UPDATE launch_trade_seen SET received_at='2000-01-01T00:00:00+00:00'")
    ls.insert_trade(c, "NEW", "M", "buy", 1.0, 1.0, "T", 1.0, "helius")  # txn still open
    assert c.in_transaction
    n = asyncio.run(ls.prune(c, keep_h=1, batch=5))
    assert n == 24
    assert c.execute("SELECT signature FROM launch_trades").fetchall() == [("NEW",)]
    assert not c.in_transaction


def test_committer_closes_idle_txn(tmp_path):
    import asyncio
    c = ls.connect(str(tmp_path / "s.db"))
    ls.insert_trade(c, "S1", "M", "buy", 1.0, 1.0, "T", 1.0, "helius")
    assert c.in_transaction

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(ls.committer(c, stop, every=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await task
    asyncio.run(go())
    assert not c.in_transaction


def test_safe_commit_survives_busy(tmp_path):
    db = str(tmp_path / "s.db")
    a, b = ls.connect(db), ls.connect(db)
    b.execute("PRAGMA busy_timeout=50")
    a.execute("BEGIN IMMEDIATE")
    a.execute("INSERT INTO launch_rpc_events VALUES ('x','p','2000',1)")

    class Busy:
        def commit(self):
            raise sqlite3.OperationalError("database is locked")
    assert ls.safe_commit(Busy()) is False
    a.commit()
    assert ls.safe_commit(b) is True


def test_open_meta_positions_extend_trade_follow(tmp_path):
    meta = tmp_path / "meta.db"
    m = sqlite3.connect(meta)
    m.execute("CREATE TABLE meta_positions (mint TEXT, status TEXT)")
    m.executemany("INSERT INTO meta_positions VALUES (?,?)", [("OPEN1", "open"), ("SHUT", "closed")])
    m.commit()
    m.close()
    ls.EXTEND.clear()
    assert ls.reload_open_positions(str(meta), now=1000.0) == 1
    assert ls.EXTEND == {"OPEN1": 1000.0 + ls.OPEN_FOLLOW_S}
    assert ls.reload_open_positions(str(tmp_path / "missing.db")) == 0
    ls.EXTEND.clear()


def test_backfill_legacy_null_source(tmp_path):
    c = ls.connect(str(tmp_path / "s.db"))
    c.executemany("INSERT INTO launch_trades (signature, mint, received_at, side, sol, tokens, trader, market_cap_sol)"
                  " VALUES (?,?,?,?,?,?,?,?)", [("A", "M", "t", "buy", 1, 1, "T", 31.1234),
                                                ("B", "M", "t", "buy", 1, 1, "T", 31.123456789),
                                                ("C", "M", "t", "buy", 1, 1, None, 31.1)])
    assert ls.backfill_legacy_source(c) == 1
    got = dict(c.execute("SELECT signature, source FROM launch_trades"))
    assert got == {"A": "helius", "B": None, "C": None}


def test_queued_writer_never_blocks_and_survives_busy(tmp_path):
    import sqlite3
    import time as _t

    db = str(tmp_path / "w.db")
    ls.connect(db).close()
    hog = sqlite3.connect(db, timeout=0)
    hog.execute("BEGIN IMMEDIATE")  # another process holds the write lock
    w = ls.QueuedWriter(db, every=0.01).start()
    t0 = _t.monotonic()
    for i in range(200):
        ls.insert_trade(w, f"s{i}", "M", "buy", 1.0, 1.0, "T", 30.0, "helius", 123 + i)
    assert _t.monotonic() - t0 < 0.5  # enqueue only: the event loop never waits on the lock
    _t.sleep(0.3)
    hog.rollback()
    hog.close()
    deadline = _t.monotonic() + 40
    while w.written < 400 and _t.monotonic() < deadline:
        _t.sleep(0.05)
    w.close()
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*), MIN(slot), MAX(slot) FROM launch_trades").fetchone() == (200, 123, 322)
    assert w.dropped == 0


def test_writer_runs_prune_on_its_thread(tmp_path):
    db = str(tmp_path / "p.db")
    ls.connect(db).close()
    w = ls.QueuedWriter(db, every=0.01).start()
    w.execute("INSERT INTO launch_trades (signature, mint, received_at) VALUES ('old','M','2000-01-01T00:00:00+00:00')")
    assert w.submit(ls.prune_sync).result(timeout=10) == 1
    w.close()


def test_rpc_trade_carries_slot_and_follow_window_is_long(tmp_path):
    assert ls.TRADE_FOLLOW_S >= 600
    c = ls.connect(str(tmp_path / "s.db"))
    assert "slot" in {r[1] for r in c.execute("PRAGMA table_info(launch_trades)")}


def test_budget_caps_helius_and_resets_daily(monkeypatch):
    day = ["2026-10-22"]
    b = ls.Budget(day_fn=lambda: day[0])
    monkeypatch.setattr(ls, "HELIUS_CREDITS_DAY", 100.0)
    assert ls.helius_credits(100_000) == 2 and ls.helius_credits(0, 5) == 5
    b.add("helius", 4_900_000)  # 98 credits
    assert b.helius_ok()
    b.add("helius", http=2, msgs=0)
    assert not b.helius_ok()
    day[0] = "2026-10-23"
    assert b.helius_ok() and b.credits() == 0


def test_usage_survives_restart(tmp_path, monkeypatch):
    db = str(tmp_path / "n.db")
    ls.connect(db).close()
    monkeypatch.setattr(ls, "HELIUS_CREDITS_DAY", 100.0)
    tr = ls.Transport(ls.Budget())
    tr.budget.add("helius", 6_000_000)  # 120 credits -> over the cap
    tr.budget.add("free", 1234)
    c = sqlite3.connect(db)
    assert ls.save_usage(tr)(c) == 2
    c.close()
    tr2 = ls.Transport(ls.Budget())  # "restart"
    ls.load_usage(db, tr2)
    assert tr2.budget.credits() == 120 and not tr2.budget.helius_ok()
    assert tr2.budget._get("free")["bytes"] == 1234
    s = ls.write_status(ls.status_path(db), tr2)
    assert s["helius"]["credits"] == 120 and "api-key" not in __import__("pathlib").Path(ls.status_path(db)).read_text()


def test_helius_only_after_free_failures_and_under_cap(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "x")
    monkeypatch.setattr(ls, "FREE_WS", ["wss://a", "wss://b"])
    monkeypatch.setattr(ls, "HELIUS_CREDITS_DAY", 10.0)
    tr = ls.Transport(ls.Budget())
    assert not tr.use_helius()
    tr.free_fail = 3
    assert not tr.use_helius()
    tr.free_fail = 4
    assert tr.use_helius()
    tr.budget.add("helius", 500_000)  # 10 credits = cap
    assert not tr.use_helius()
    monkeypatch.delenv("HELIUS_API_KEY")
    tr.budget = ls.Budget()
    assert not tr.use_helius()


def test_get_tx_free_first_then_capped_helius(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "x")
    monkeypatch.setattr(ls, "FREE_HTTP", ["https://free1", "https://free2"])
    monkeypatch.setattr(ls, "HELIUS_CREDITS_DAY", 1.0)
    calls = []

    def post(url, body):
        calls.append(url)
        if "helius" in url:
            return {"ok": "helius"}
        raise OSError("429")
    monkeypatch.setattr(ls, "_post", post)
    tr = ls.Transport(ls.Budget())
    assert ls._get_tx("S", tr) == {"ok": "helius"}
    assert [u.split("/")[2] for u in calls] == ["free1", "free2", "mainnet.helius-rpc.com"]
    assert tr.budget.credits() == 1 and tr.budget._get("free")["http"] == 2
    try:  # cap reached: Helius is not called again, the free error surfaces
        ls._get_tx("S2", tr)
        raise AssertionError("expected the free-node error")
    except OSError:
        pass
    assert sum("helius" in u for u in calls) == 1
    monkeypatch.setattr(ls, "_post", lambda url, body: {"ok": url})
    assert ls._get_tx("S3", tr) == {"ok": "https://free1"}


def test_run_rpc_falls_back_to_helius_then_auto_stops(monkeypatch):
    import asyncio
    monkeypatch.setenv("HELIUS_API_KEY", "x")
    monkeypatch.setattr(ls, "FREE_WS", ["wss://free"])
    monkeypatch.setattr(ls, "HELIUS_CREDITS_DAY", 4.0)
    seen = []

    async def fake_session(url, src, c, stop, llq=None, tr=None, until=None):
        seen.append(src)
        if len(seen) >= 8:
            stop.set()
        if src == "free":
            raise OSError("down")
        tr.budget.add("helius", 250_000)  # 5 credits > cap
        tr.stopped = "helius daily budget reached"

    async def nosleep(_s):
        return None
    monkeypatch.setattr(ls, "rpc_session", fake_session)
    monkeypatch.setattr(ls.asyncio, "sleep", nosleep)

    class W:
        def commit(self):
            pass
    tr = ls.Transport(ls.Budget())
    asyncio.run(ls.run_rpc(W(), asyncio.Event(), None, tr))
    assert seen[:3] == ["free", "free", "helius"]  # 2 x len(FREE_WS) free failures, then one Helius session
    assert seen.count("helius") == 1 and tr.stopped  # capped: never back on Helius today


def test_heartbeat_shows_transport(tmp_path):
    from noema import heartbeats as hb
    db = str(tmp_path / "noema.db")
    assert hb._launch_transport(db) == ""
    tr = ls.Transport(ls.Budget())
    tr.mode = "free"
    ls.write_status(ls.status_path(db), tr)
    assert "free public node" in hb._launch_transport(db)
