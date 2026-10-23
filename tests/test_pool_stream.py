import base64
import gzip
import json
import os
import sqlite3
import struct
import time

from noema import heartbeats as hb
from noema import pool_stream as ps
from noema.launch_stream import WSOL, QueuedWriter, b58

A = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58d(s):
    n = 0
    for ch in s:
        n = n * 58 + A.index(ch)
    return n.to_bytes(32, "big")


K = {n: bytes([n]) * 32 for n in range(1, 9)}   # fake 32-byte keys


def create_pool_ev(pool=K[1], mint=K[2], quote=None, pool_base=206_900_000_000_000, pool_quote=84_990_359_056,
                   ts=1_792_722_896):
    quote = quote or b58d(WSOL)
    d = bytearray(344)
    d[:8] = ps.CREATE_POOL
    struct.pack_into("<qH", d, 8, ts, 0)
    d[18:50], d[50:82], d[82:114] = K[3], mint, quote
    d[114], d[115] = 6, 9
    for i, v in enumerate([0, 0, pool_base, pool_quote, 0, 0, 0]):
        struct.pack_into("<Q", d, 116 + 8 * i, v)
    d[173:205], d[205:237] = pool, K[4]
    return bytes(d)


def swap_ev(side="buy", base=1_000_000_000, quote=500_000_000, pool_base=180_000_000_000_000,
            pool_quote=85_000_000_000, pool=K[1], user=K[5], ts=1_792_722_900):
    d = bytearray(300)
    d[:8] = ps.BUY if side == "buy" else ps.SELL
    struct.pack_into("<q", d, 8, ts)
    u = [base, 0, 0, 0, pool_base, pool_quote, quote, 20, quote // 500, 5, quote // 2000, quote, quote]
    for i, v in enumerate(u):
        struct.pack_into("<Q", d, 16 + 8 * i, v)
    d[120:152], d[152:184] = pool, user
    return bytes(d)


def logs(*events, migrate=False):
    out = ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]"]
    if migrate:
        out.append("Program log: Instruction: Migrate")
    return out + ["Program data: " + base64.b64encode(e).decode() for e in events]


class W:
    def __init__(self):
        self.rows = []

    def execute(self, sql, args=()):
        self.rows.append((sql, args))


def test_decoders_roundtrip():
    ev = ps.decode_create_pool(create_pool_ev())
    assert ev["pool"] == b58(K[1]) and ev["mint"] == b58(K[2]) and ev["quote_mint"] == WSOL
    assert ev["pool_base"] == 206_900_000_000_000 and ev["pool_quote"] == 84_990_359_056
    s = ps.decode_swap(swap_ev("sell"))
    assert s["side"] == "sell" and s["pool"] == b58(K[1]) and s["trader"] == b58(K[5]) and s["base"] == 1_000_000_000
    assert ps.decode_swap(create_pool_ev()) is None and ps.decode_create_pool(swap_ev()) is None
    assert ps.decode_swap(swap_ev()[:100]) is None


def test_migration_requires_migrate_instruction():
    assert ps.migration_from_logs(logs(create_pool_ev())) == []
    got = ps.migration_from_logs(logs(create_pool_ev(), migrate=True))
    assert len(got) == 1 and got[0]["pool"] == b58(K[1])


def test_quality_rules():
    ok = ps.decode_swap(swap_ev(base=1_000_000_000, quote=int(85e9 * 1e9 / (180e12 - 1e9) * 1.1)))
    assert ps.quality(ok) is None                         # 10% over x*y=k is normal on graduated pools
    assert ps.quality(ok, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") == "non_sol_quote"
    drained = ps.decode_swap(swap_ev("sell", base=1_000_000_000, quote=int(85e9 * 1e9 / (180e12 + 1e9) * 1.5)))
    assert ps.quality(drained) is None                    # ~50% premium seen live on a drained pool
    bad = ps.decode_swap(swap_ev(base=1_000_000_000, quote=5_000_000_000))
    assert "off_reserves" in ps.quality(bad)
    assert "low_liq" in ps.quality(ps.decode_swap(swap_ev(pool_quote=100_000_000)))
    assert "zero_sol" in ps.quality(ps.decode_swap(swap_ev(quote=0)))
    assert ps.migration_quality(WSOL, 84.99) is None
    assert ps.migration_quality(WSOL, 0.12) == "nonstandard"
    assert ps.migration_quality("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 12) == "non_sol_quote"


def test_trade_row_prices():
    r = ps.trade_row(ps.decode_swap(swap_ev(base=2_000_000, quote=1_000_000)), "sig", 0, "mint", 7, "free_ws")
    assert len(r) == 23 and r[8] == "buy" and r[9] == 0.001 and r[10] == 2.0
    assert abs(r[12] - 0.0005) < 1e-12 and abs(r[13] - 0.0005 * 1e9) < 1e-3   # mcap = price x 1B


def test_budget_credits_and_rollover():
    assert ps.helius_credits(100_000) == 2 and ps.helius_credits(0, 5) == 5
    day = ["2026-10-22"]
    b = ps.Budget(day_fn=lambda: day[0])
    b.add("helius", nbytes=int(ps.HELIUS_CREDITS_DAY / 2 * 1e5))
    assert not b.helius_ok()
    day[0] = "2026-10-23"
    assert b.helius_ok() and b.credits() == 0
    b.load("free", 5, 10, 3)
    assert b._get("free") == {"msgs": 5, "bytes": 10, "http": 3}


def test_register_and_handle_dedupe_and_cap():
    st, w = ps.State(), W()
    mig = ps.migration_from_logs(logs(create_pool_ev(), migrate=True))[0]
    assert ps.register_migration(w, st, mig, "msig", 1, "free_ws", now=1_792_722_900)
    assert not ps.register_migration(w, st, mig, "msig", 1, "free_ws", now=1_792_722_900)
    other = swap_ev(pool=K[7])                            # not followed -> ignored
    n = ps.handle_pool_logs(w, st, "tsig", logs(swap_ev(), swap_ev("sell"), other), 9, "free_ws")
    assert n == 2 and st.pools[b58(K[1])]["first_slot"] == 9
    trades = [a for s, a in w.rows if s == ps.INSERT_TRADE]
    assert [(t[0], t[2]) for t in trades] == [("tsig", 0), ("tsig", 1)]
    assert ps.handle_pool_logs(w, st, "z", logs(swap_ev(quote=0)), 9, "free_ws") == 0 and st.n["dropped"] == 1
    assert st.desired(10, now=1_792_722_900) == [b58(K[1])]
    assert st.desired(10, now=1_792_722_900 + ps.FOLLOW_S + 5) == []


def test_writer_connect_fn_and_schema(tmp_path):
    db = str(tmp_path / "pool_trades.db")
    w = QueuedWriter(db, connect_fn=ps.connect)
    w.start()
    st = ps.State()
    mig = ps.migration_from_logs(logs(create_pool_ev(), migrate=True))[0]
    ps.register_migration(w, st, mig, "msig", 1, "free_ws")
    ps.handle_pool_logs(w, st, "tsig", logs(swap_ev()), 2, "free_ws")
    ps.handle_pool_logs(w, st, "tsig", logs(swap_ev()), 2, "free_http")   # replay = PK dedupe
    deadline = time.time() + 10
    while time.time() < deadline:
        c = sqlite3.connect(db)
        try:
            n = c.execute("SELECT COUNT(*) FROM pool_trades").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        if n:
            break
        time.sleep(0.1)
    time.sleep(0.3)
    assert c.execute("SELECT COUNT(*) FROM pool_trades").fetchone()[0] == 1
    assert c.execute("SELECT quality FROM pool_migrations").fetchone()[0] is None
    c.close()
    w.close()
    st2 = ps.State()
    ps.load_state(db, st2)
    assert b58(K[1]) in st2.pools and st2.pools[b58(K[1])]["head_done"]


def test_import_archive_readonly(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    with gzip.open(root / "epoch-900.other.jsonl.gz", "wt") as fh:
        fh.write(json.dumps({"kind": "migrate", "pool": "P1", "mint": "M1", "signature": "s", "slot": 5,
                             "timestamp": 1, "quote_mint": WSOL, "mint_amount": 2e14, "sol_amount": 85e9}) + "\n")
        fh.write(json.dumps({"kind": "trade", "mint": "M2"}) + "\n")
    db = str(tmp_path / "pt.db")
    assert ps.import_archive(db, str(root)) == 1
    assert ps.import_archive(db, str(root)) == 0


def test_heartbeat_probe(tmp_path):
    main = str(tmp_path / "noema.db")
    sqlite3.connect(main).close()
    c = ps.connect(str(tmp_path / "pool_trades.db"))
    c.execute(ps.INSERT_TRADE, ps.trade_row(ps.decode_swap(swap_ev()), "s", 0, "m", 1, "free_ws"))
    c.commit()
    c.close()
    st = ps.State()
    ps.write_status(ps.status_path(str(tmp_path / "pool_trades.db")), st, 3)
    r = hb._pool_trades(main)
    assert r["name"] == "feed:pool_trades" and r["error"] is None and r["success"]
    assert "following 3/" in r["detail"] and "helius" in r["detail"]
    st.stopped = "free-RPC daily message/byte budget reached"
    ps.write_status(ps.status_path(str(tmp_path / "pool_trades.db")), st, 0)
    r = hb._pool_trades(main)
    assert r["error"] and r["success"] is None
    assert "agent:pool_stream" in hb.LOOPS


def test_helius_url_never_in_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "SECRETKEY123")
    p = str(tmp_path / "s.json")
    ps.write_status(p, ps.State(), 0)
    with open(p) as fh:
        assert "SECRETKEY123" not in fh.read()
    assert os.stat(p).st_mode & 0o777 == 0o644


def test_hot_pool_duty_cycle():
    now = 1_000.0
    v = {"w0": now - 30, "wb": int(ps.HOT_MB_MIN * 1e6 / 2 * 4)}      # 4x the rate over 30 s
    off = ps.hot_off_s(v, now)
    assert abs(off - ps.HOT_ON_S * 3) < 1e-6 and v["hot"] and v["wb"] == 0
    v = {"w0": now - 30, "wb": int(ps.HOT_MB_MIN * 1e6 * 100)}       # way over -> capped
    assert ps.hot_off_s(v, now) == ps.HOT_MAX_OFF_S
    quiet = {"w0": now - 61, "wb": 1000}
    assert ps.hot_off_s(quiet, now) == 0 and quiet["wb"] == 0 and quiet["w0"] == now
    young = {"w0": now - 5, "wb": 10**9}                              # not measured long enough yet
    assert ps.hot_off_s(young, now) == 0
    st = ps.State()
    st.pools["P"] = {"at": now - 10, "quote_mint": WSOL, "off_until": now + 50}
    assert st.desired(5, now=now) == [] and st.desired(5, now=now + 51) == ["P"]


def test_sample_evenly_and_tail_selection():
    assert ps.sample_evenly([1, 2, 3], 8) == [1, 2, 3]
    s = ps.sample_evenly(list(range(100)), 5)
    assert s[0] == 0 and s[-1] == 99 and len(s) == 5
    st = ps.State()
    now = 1_000_000.0
    st.pools = {"live": {"at": now - 60, "quote_mint": WSOL}, "tail": {"at": now - 7200, "quote_mint": WSOL},
                "old": {"at": now - ps.TAIL_S - 700, "quote_mint": WSOL},
                "usdc": {"at": now - 7200, "quote_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}}
    assert st.tail_pools(now) == ["tail"]
    assert not st.tail_active("tail", now)                            # no trade for 2 h -> dead cadence
    st.pools["tail"]["last_trade"] = now - 60
    assert st.tail_active("tail", now)
    st.prune(now)
    assert "tail" in st.pools and "old" not in st.pools


def test_tail_budget_pacing():
    day = ["2026-10-23"]
    b = ps.Budget(day_fn=lambda: day[0])
    midnight = 1_792_713_600.0                       # 2026-10-23 00:00 UTC
    assert midnight % 86400 == 0
    b.add("tail", http=int(ps.TAIL_HTTP_DAY * 0.03), msgs=0)
    assert b.tail_paced(midnight)                    # 30 min slack = 2.1% of the day; 3% is ahead of pace
    assert not b.tail_paced(midnight + 3600)         # 1.5 h in -> 6.25% allowed
    b.add("tail", http=ps.TAIL_HTTP_DAY, msgs=0)
    assert not b.tail_ok()


class FakeHttp:
    def __init__(self, sigs, txs):
        self.sigs, self.txs, self.calls = sigs, txs, []

    async def call(self, method, params):
        self.calls.append((method, params))
        return self.sigs if method == "getSignaturesForAddress" else self.txs.get(params[0])


def test_tail_poll_skips_failed_and_writes_rows():
    import asyncio
    st, w = ps.State(), W()
    pool = b58(K[1])
    st.pools[pool] = {"mint": "M", "at": 1_000.0, "quote_mint": WSOL, "last_sig": "cur", "last_slot": 5}
    sigs = [{"signature": "s3", "slot": 9, "err": None, "blockTime": 5_000},
            {"signature": "s2", "slot": 8, "err": {"InstructionError": [3, {"Custom": 6004}]}, "blockTime": 4_990},
            {"signature": "s1", "slot": 7, "err": None, "blockTime": 4_980}]
    txs = {s: {"slot": sl, "meta": {"err": None, "logMessages": logs(swap_ev())}} for s, sl in (("s1", 7), ("s3", 9))}
    http = FakeHttp(sigs, txs)
    n = asyncio.run(ps.tail_poll(http, w, st, pool, now=5_010.0))
    assert n == 2
    assert http.calls[0][1][1]["until"] == "cur"
    assert [c[1][0] for c in http.calls[1:]] == ["s1", "s3"]          # failed s2 never fetched
    rows = [a for s, a in w.rows if s == ps.INSERT_TRADE]
    assert {r[0] for r in rows} == {"s1", "s3"} and all(r[18] == "tail_poll" for r in rows)
    cov = [a for s, a in w.rows if s == ps.INSERT_COV]
    assert cov == [(pool, 1_000.0 + ps.FOLLOW_S, 5_010.0, "tail_poll", 1, 2, 2)]
    p = st.pools[pool]
    assert p["last_sig"] == "s3" and p["last_trade"] == 1_792_722_900 and p["tail_next"] == 5_010.0 + ps.TAIL_ACTIVE_S


def test_load_state_resumes_tail_cursor(tmp_path):
    db = str(tmp_path / "pt.db")
    c = ps.connect(db)
    now = time.time()
    c.execute(ps.INSERT_MIG, ("P", "M", "ms", 1, int(now - 5 * 3600), ps._now(), "free_ws", ps.PUMP_AMM, WSOL,
                              1.0, 85.0, None, None, None))
    row = list(ps.trade_row(ps.decode_swap(swap_ev(pool=b58d("11111111111111111111111111111112"))), "lastsig", 0,
                            "M", 77, "free_ws"))
    row[1] = "P"
    c.execute(ps.INSERT_TRADE, row)
    c.execute(ps.INSERT_COV, ("P", now - 4 * 3600 - 60, now - 4 * 3600, "free_ws", 1, None, None))
    c.commit()
    c.close()
    st = ps.State()
    ps.load_state(db, st)
    v = st.pools["P"]
    assert v["last_sig"] == "lastsig" and v["last_slot"] == 77 and abs(v["tail_from"] - (now - 4 * 3600)) < 1
    assert st.tail_pools() == ["P"]


def swap_ev_v(side="buy", base=1_000_000_000, pool_base=180_000_000_000_000, pool_quote=85_000_000_000,
              virtual=17_600_000_000, fee=1.0, ix="buy"):
    """Current-layout event with virtual_quote_reserves; quote priced on vault+virtual (x fee)."""
    den = pool_base - base if side == "buy" else pool_base + base
    quote = int((pool_quote + virtual) * base / den * fee)
    d = bytearray(swap_ev(side, base, quote, pool_base, pool_quote).ljust(360, b"\0"))
    if side == "buy":
        d += bytes(1 + 5 * 8) + struct.pack("<I", len(ix)) + ix.encode()
    d += bytes(4 * 8) + virtual.to_bytes(16, "little", signed=True) + bytes(1 + 4 * 8 + 8)
    return bytes(d)


def test_virtual_quote_reserves_decoded_and_priced():
    for side, ix in (("buy", "buy"), ("buy", "buy_exact_quote_in"), ("sell", "")):
        ev = ps.decode_swap(swap_ev_v(side, ix=ix))
        assert ev["virtual_quote"] == 17_600_000_000
        assert ps.quality(ev) is None                    # exact vs vault+virtual
        r = ps.trade_row(ev, "s", 0, "m", 1, "free_ws")
        assert r[20] == 17.6 and r[22] == "vault+virtual"
        assert abs(r[21] - (85 + 17.6) / 180_000_000) < 1e-15   # (vault quote + virtual) / vault base
    assert ps.quality(ps.decode_swap(swap_ev_v(base=50_000_000_000, fee=1.0125))) is None   # buy-exact-quote-in: fill = curve x fee
    # a 30% premium over vault-only was "normal" before; vs vault+virtual it is garbage
    assert "off_reserves" in ps.quality(ps.decode_swap(swap_ev_v(base=50_000_000_000, fee=1.3)))
    assert ps.decode_swap(swap_ev())["virtual_quote"] is None   # legacy layout
    r = ps.trade_row(ps.decode_swap(swap_ev()), "s", 0, "m", 1, "free_ws")
    assert r[20] is None and r[21] is None and r[22] == "vault_only_event"
    neg = ps.decode_swap(swap_ev_v(virtual=-2_000_000_000))
    assert neg["virtual_quote"] == -2_000_000_000 and ps.quality(neg) is None


def test_connect_migrates_legacy_table(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE pool_trades (signature TEXT NOT NULL, pool TEXT NOT NULL, n INTEGER NOT NULL,
      mint TEXT NOT NULL, program TEXT NOT NULL, slot INTEGER, block_ts INTEGER, received_at TEXT NOT NULL,
      side TEXT NOT NULL, sol REAL, tokens REAL, trader TEXT, price REAL, mcap_sol REAL, pool_base REAL,
      pool_quote_sol REAL, lp_fee_sol REAL, protocol_fee_sol REAL, source TEXT, quality TEXT,
      PRIMARY KEY (signature, pool, n))""")
    c.execute("INSERT INTO pool_trades VALUES (" + ",".join("?" * 20) + ")",
              ("old", "P", 0, "M", "pump_amm", 1, 1, "t", "buy", 1, 1, "u", 1, 1, 1, 1, 0, 0, "free_ws", None))
    c.commit()
    c.close()
    c = ps.connect(db)
    c.execute(ps.INSERT_TRADE, ps.trade_row(ps.decode_swap(swap_ev_v()), "new", 0, "M", 2, "free_ws"))
    got = dict(c.execute("SELECT signature, reserve_basis FROM pool_trades").fetchall())
    assert got == {"old": "legacy_vault_only", "new": "vault+virtual"}
    c.close()
    ps.connect(db).close()                               # idempotent


def test_hot_threshold_shrinks_with_hot_pools():
    assert ps.hot_threshold(0) == ps.HOT_MB_MIN and ps.hot_threshold(1) == ps.HOT_MB_MIN
    assert ps.hot_threshold(5) == ps.HOT_TOTAL_MB_MIN / 5
    assert ps.hot_threshold(1000) == ps.HOT_MIN_MB_MIN


def test_http_429_cools_endpoint_and_rotates(monkeypatch):
    import asyncio
    import urllib.error
    ps._COOL.clear()
    a, b = ps.FREE_HTTP[0], ps.FREE_HTTP[1]
    calls = []

    def fake_rpc(url, method, params, meter=None):
        calls.append(url)
        if url == a:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "40"}, None)
        meter[0] += 100
        return {"ok": url}

    monkeypatch.setattr(ps, "_rpc", fake_rpc)
    st = ps.State()
    h = ps.Http(st, "tail", helius_fallback=True, gap_s=0)
    assert asyncio.run(h.call("getSlot", [])) == {"ok": b}
    assert ps.free_endpoints() == [b] and ps._COOL[a][1] == 40
    calls.clear()
    for _ in range(3):
        asyncio.run(h.call("getSlot", []))
    assert calls == [b, b, b]                      # cooled endpoint skipped
    assert st.budget._get("tail")["bytes"] == 400 and st.n["http_429"] == 1
    monkeypatch.setattr(ps.time, "time", lambda: 1e12)
    assert set(ps.free_endpoints()) == set(ps.FREE_HTTP)   # cooldown expires
    ps._COOL.clear()
