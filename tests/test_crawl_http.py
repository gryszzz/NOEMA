import sqlite3
import threading
import time

import httpx

from noema import news_crawler as nc
from noema.crawl_http import (
    HostGate,
    SweepMeter,
    adaptive_sleep,
    fetch_concurrently,
    retry_after_seconds,
)


def test_retry_after_bounds():
    assert retry_after_seconds("30") == 30
    assert retry_after_seconds(None) == 60
    assert retry_after_seconds("999999") == 1800
    assert retry_after_seconds("garbage") == 60


def test_fetch_concurrently_keeps_order_and_parallelizes():
    def slow(i):
        time.sleep(0.1)
        return i * 2
    t0 = time.monotonic()
    assert fetch_concurrently(slow, range(8), workers=8) == [i * 2 for i in range(8)]
    assert time.monotonic() - t0 < 0.5


def test_gate_serializes_one_host():
    gate, active, peak, lock = HostGate(0.0), [0], [0], threading.Lock()

    def hit(_):
        sem = gate.acquire("h")
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        sem.release()
    fetch_concurrently(hit, range(6), workers=6)
    assert peak[0] == 1


def test_429_cools_host_and_meter_counts(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(429, headers={"retry-after": "120"})
    conn = sqlite3.connect(tmp_path / "n.db")
    nc.ensure_schema(conn)
    pc = nc.PoliteClient(conn, client=httpx.Client(transport=httpx.MockTransport(handler)), spacing=0)
    pc.meter = m = SweepMeter("news")
    assert pc.get("https://x.test/a")[0] == 429
    assert pc.get("https://x.test/b")[0] == 429  # cooling: not sent
    assert sum("/a" in c or "/b" in c for c in calls) == 1
    s = m.save(conn, 0)
    assert s["rate_limited"] == 1 and s["requests"] == 1
    assert conn.execute("SELECT COUNT(*) FROM crawl_sweeps").fetchone()[0] == 1


def test_adaptive_intervals(tmp_path):
    conn = sqlite3.connect(tmp_path / "n.db")
    nc.ensure_schema(conn)
    assert nc.adaptive_feed_interval(conn, "x", 300, 5) == 120
    assert nc.adaptive_feed_interval(conn, "x", 300, 0) == 600
    assert adaptive_sleep(20, 300, floor=180, ceiling=600, hot=10) == 180
    assert adaptive_sleep(0, 300, floor=180, ceiling=600, hot=10) == 600


def test_live_feed_ring(tmp_path):
    from noema.crawl_http import LiveFeed, crawl_live
    db = str(tmp_path / "l.db")
    lf = LiveFeed(db, "news")
    for i in range(60):
        lf.finish(lf.start("h.test", f"s{i}"), 200, 10)
    lf.items("s59", 3)
    tok = lf.start("h.test", "inflight")
    out = crawl_live(db)
    assert out["schema"] == "tev.crawl_live.v1" and len(out["entries"]) == 50
    assert out["in_flight"] == 1 and out["entries"][1]["items"] == 3
    lf.finish(tok, 304, 0)
