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
    assert len(r) == 20 and r[8] == "buy" and r[9] == 0.001 and r[10] == 2.0
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
