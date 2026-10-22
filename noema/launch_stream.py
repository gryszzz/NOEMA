"""Free live new-launch stream (PAPER / READ ONLY): PumpPortal websocket + Solana RPC logsSubscribe backup.

* PumpPortal (wss://pumpportal.fun/api/data, free, no key): `subscribeNewToken` -> every pump.fun create;
  each new mint's trades are followed for TRADE_FOLLOW_S via `subscribeTokenTrade` (capped concurrency).
* Solana RPC websocket `logsSubscribe` (mentions pump.fun / Raydium LaunchLab programs) as an independent
  backup detector, keyed by transaction signature. Uses HELIUS_API_KEY from the environment if present
  (never printed or stored), else the public mainnet endpoint.
Writes data/noema.db tables launch_stream / launch_trades / launch_rpc_events. Nothing here signs or trades.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

PUMPPORTAL = "wss://pumpportal.fun/api/data"
PROGRAMS = {"pump.fun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            "raydium_launchlab": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"}
CREATE_MARKERS = ("Instruction: Create", "Instruction: InitializeV2", "Instruction: Initialize")
TRADE_FOLLOW_S = int(os.getenv("NOEMA_STREAM_TRADE_FOLLOW_S", "180"))
MAX_FOLLOW = int(os.getenv("NOEMA_STREAM_MAX_FOLLOW", "150"))
# PumpPortal only serves per-token trades to an API key funded with >= 0.02 SOL (paid) - off by default.
FOLLOW_TRADES = os.getenv("NOEMA_STREAM_TRADES", "0") == "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS launch_stream (mint TEXT PRIMARY KEY, signature TEXT, received_at TEXT NOT NULL,
  source TEXT NOT NULL, name TEXT, symbol TEXT, creator TEXT, initial_sol REAL, market_cap_sol REAL, raw_json TEXT);
CREATE INDEX IF NOT EXISTS launch_stream_at ON launch_stream(received_at);
CREATE TABLE IF NOT EXISTS launch_trades (signature TEXT PRIMARY KEY, mint TEXT NOT NULL, received_at TEXT NOT NULL,
  side TEXT, sol REAL, tokens REAL, trader TEXT, market_cap_sol REAL);
CREATE INDEX IF NOT EXISTS launch_trades_mint ON launch_trades(mint, received_at);
CREATE TABLE IF NOT EXISTS launch_rpc_events (signature TEXT PRIMARY KEY, program TEXT, received_at TEXT NOT NULL,
  slot INTEGER);
"""


def rpc_ws_url() -> str:
    key = os.getenv("HELIUS_API_KEY")
    if key:
        return f"wss://mainnet.helius-rpc.com/?api-key={key}"
    return os.getenv("NOEMA_SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com")


def connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def _now() -> str:
    return datetime.now(UTC).isoformat()


def handle_pumpportal(c: sqlite3.Connection, msg: dict[str, Any], follow: dict[str, float]) -> str | None:
    """Returns a mint to subscribe trades for (new create), else None."""
    tx = msg.get("txType")
    if tx == "create" and msg.get("mint"):
        c.execute("INSERT OR IGNORE INTO launch_stream VALUES (?,?,?,?,?,?,?,?,?,?)", (
            msg["mint"], msg.get("signature"), _now(), "pumpportal", (msg.get("name") or "")[:80],
            (msg.get("symbol") or "")[:24], msg.get("traderPublicKey"), msg.get("solAmount"), msg.get("marketCapSol"),
            json.dumps({k: msg.get(k) for k in ("uri", "pool", "bondingCurveKey", "initialBuy")})))
        follow[msg["mint"]] = time.time()
        return msg["mint"]
    if tx in ("buy", "sell") and msg.get("mint") and msg.get("signature"):
        c.execute("INSERT OR IGNORE INTO launch_trades VALUES (?,?,?,?,?,?,?,?)", (
            msg["signature"], msg["mint"], _now(), tx, msg.get("solAmount"), msg.get("tokenAmount"),
            msg.get("traderPublicKey"), msg.get("marketCapSol")))
    return None


async def run_pumpportal(c: sqlite3.Connection, stop: asyncio.Event) -> None:
    import websockets
    follow: dict[str, float] = {}
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(PUMPPORTAL, ping_interval=20, max_size=2**22) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                backoff, last_commit = 1.0, time.time()
                while not stop.is_set():
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    new = handle_pumpportal(c, msg, follow)
                    if FOLLOW_TRADES and new and len(follow) <= MAX_FOLLOW:
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [new]}))
                    old = [m for m, t in follow.items() if time.time() - t > TRADE_FOLLOW_S]
                    if old and FOLLOW_TRADES:
                        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": old}))
                        for m in old:
                            follow.pop(m, None)
                    if time.time() - last_commit > 2:
                        c.commit()
                        last_commit = time.time()
        except Exception as exc:  # noqa: BLE001 - reconnect with backoff
            print(_now(), "pumpportal", type(exc).__name__, str(exc)[:120], flush=True)
            c.commit()
            follow.clear()
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)


async def run_rpc(c: sqlite3.Connection, stop: asyncio.Event) -> None:
    import websockets
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(rpc_ws_url(), ping_interval=20, max_size=2**23) as ws:
                for i, (_name, prog) in enumerate(PROGRAMS.items()):
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                                              "params": [{"mentions": [prog]}, {"commitment": "processed"}]}))
                subs: dict[int, str] = {}
                backoff, last_commit = 1.0, time.time()
                while not stop.is_set():
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    if "result" in m and isinstance(m.get("id"), int):
                        subs[m["result"]] = list(PROGRAMS)[m["id"] - 1]
                        continue
                    p = (m.get("params") or {})
                    v = ((p.get("result") or {}).get("value") or {})
                    logs = v.get("logs") or []
                    if v.get("err") is None and any(any(k in ln for k in CREATE_MARKERS) for ln in logs):
                        c.execute("INSERT OR IGNORE INTO launch_rpc_events VALUES (?,?,?,?)", (
                            v.get("signature"), subs.get(p.get("subscription"), "?"), _now(),
                            ((p.get("result") or {}).get("context") or {}).get("slot")))
                    if time.time() - last_commit > 2:
                        c.commit()
                        last_commit = time.time()
        except Exception as exc:  # noqa: BLE001
            print(_now(), "rpc", type(exc).__name__, flush=True)  # never print the URL (may hold a key)
            c.commit()
            await asyncio.sleep(backoff)
            backoff = min(120.0, backoff * 2)


def latency_report(db: str, hours: float = 6) -> dict[str, Any]:
    """Detection latency vs on-chain pool time (trench_launches.first_pool_at) for the same mints."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    since = datetime.fromtimestamp(time.time() - hours * 3600, UTC).isoformat()
    def q(sql: str) -> list[float]:
        try:
            return sorted(r[0] for r in c.execute(sql, (since,)) if r[0] is not None)
        except sqlite3.Error:
            return []
    poll = q("SELECT (julianday(discovered_at)-julianday(first_pool_at))*86400 FROM trench_launches WHERE first_pool_at>=?")
    stream = q("SELECT (julianday(s.received_at)-julianday(t.first_pool_at))*86400 FROM launch_stream s JOIN trench_launches t"
               " USING (mint) WHERE t.first_pool_at>=?")
    lead = q("SELECT (julianday(t.discovered_at)-julianday(s.received_at))*86400 FROM launch_stream s JOIN trench_launches t"
             " USING (mint) WHERE s.received_at>=?")
    rpc_vs_pp = q("SELECT (julianday(s.received_at)-julianday(r.received_at))*86400 FROM launch_stream s JOIN"
                  " launch_rpc_events r ON r.signature=s.signature WHERE s.received_at>=?")
    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE received_at>=?", (since,)).fetchone()[0]
              for t in ("launch_stream", "launch_trades", "launch_rpc_events") if _has(c, t)}
    c.close()

    def s(xs: list[float]) -> dict[str, Any]:
        return {"n": len(xs), "p50_s": round(xs[len(xs) // 2], 2) if xs else None,
                "p90_s": round(xs[int(len(xs) * 0.9)], 2) if xs else None}
    return {"hours": hours, "counts": counts, "poll_discovery_vs_pool": s(poll), "stream_vs_pool": s(stream),
            "stream_lead_over_poll": s(lead), "pumpportal_minus_rpc": s(rpc_vs_pp)}


def _has(c: sqlite3.Connection, t: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None


def prune(db: str, keep_h: float = 72) -> None:
    c = connect(db)
    cut = datetime.fromtimestamp(time.time() - keep_h * 3600, UTC).isoformat()
    for t in ("launch_trades", "launch_rpc_events"):
        c.execute(f"DELETE FROM {t} WHERE received_at < ?", (cut,))
    c.commit()
    c.close()


async def _main(db: str) -> None:
    stop = asyncio.Event()

    async def housekeeping() -> None:
        while True:
            await asyncio.sleep(3600)
            prune(db)
            print(_now(), json.dumps(latency_report(db, 1)), flush=True)
    c = connect(db)  # one connection shared by both tasks (single thread) - no self-locking
    await asyncio.gather(run_pumpportal(c, stop), run_rpc(c, stop), housekeeping())


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        print(json.dumps(latency_report(a.db), indent=1))
        return
    asyncio.run(_main(a.db))


if __name__ == "__main__":  # pragma: no cover
    main()
