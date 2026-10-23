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
import base64
import hashlib
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from .sqlite_util import connect as sqlite_connect
from .sqlite_util import is_busy

PUMPPORTAL = "wss://pumpportal.fun/api/data"
PROGRAMS = {"pump.fun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            "raydium_launchlab": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"}
CREATE_MARKERS = ("Instruction: Create", "Instruction: InitializeV2", "Instruction: Initialize")
TRADE_FOLLOW_S = int(os.getenv("NOEMA_STREAM_TRADE_FOLLOW_S", "180"))
ALIVE_S = float(os.getenv("NOEMA_STREAM_ALIVE_S", "30"))  # log heartbeat cadence (agent:launch_stream is 60s)
COMMIT_S = float(os.getenv("NOEMA_STREAM_COMMIT_S", "0.5"))  # short write txns: noema.db has many writers
MAX_FOLLOW = int(os.getenv("NOEMA_STREAM_MAX_FOLLOW", "150"))
# PumpPortal per-token trades are METERED (0.01 SOL / 10k msgs, key wallet must hold >= 0.02 SOL). Off by default:
# the free Helius logsSubscribe decoder below already sees every pump.fun bonding-curve trade.
FOLLOW_TRADES = os.getenv("NOEMA_STREAM_TRADES", "0") == "1"
# Free per-coin trades: decode pump.fun Anchor TradeEvent/CreateEvent from the same logsSubscribe stream
# (no extra RPC calls -> free-tier friendly). Trades kept only for mints created < TRADE_FOLLOW_S ago.
RPC_TRADES = os.getenv("NOEMA_RPC_TRADES", "1") == "1"
FOLLOW: dict[str, float] = {}  # mint -> first-seen epoch, shared by PumpPortal + RPC creates
_DISC = {hashlib.sha256(f"event:{n}".encode()).digest()[:8]: n for n in ("TradeEvent", "CreateEvent")}
_LL_TRADE = hashlib.sha256(b"event:TradeEvent").digest()[:8]
_LL_CREATE = hashlib.sha256(b"event:PoolCreateEvent").digest()[:8]
WSOL = "So11111111111111111111111111111111111111112"
LL_POOLS: dict[str, dict[str, Any]] = {}  # LaunchLab pool_state -> {"mint", "at"} (SOL-quoted pools only)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b: bytes) -> str:
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def decode_event(data: bytes) -> dict[str, Any] | None:
    """pump.fun Anchor events (borsh). TradeEvent: mint,sol,tokens,is_buy,user,ts,vsol,vtok,...; CreateEvent: name,symbol,uri,mint,curve,user."""
    kind = _DISC.get(data[:8])
    try:
        if kind == "TradeEvent" and len(data) >= 113:
            sol, tok = int.from_bytes(data[40:48], "little"), int.from_bytes(data[48:56], "little")
            vsol, vtok = int.from_bytes(data[97:105], "little"), int.from_bytes(data[105:113], "little")
            return {"kind": "trade", "mint": b58(data[8:40]), "sol": sol / 1e9, "tokens": tok / 1e6,
                    "side": "buy" if data[56] else "sell", "trader": b58(data[57:89]),
                    "market_cap_sol": round(vsol * 1e6 / vtok, 4) if vtok else None}
        if kind == "CreateEvent":
            o = 8
            for _ in range(3):
                o += 4 + int.from_bytes(data[o:o + 4], "little")
            return {"kind": "create", "mint": b58(data[o:o + 32]), "creator": b58(data[o + 64:o + 96])}
    except (IndexError, ValueError):
        return None
    return None


WATCH: set[str] = set()          # smart-wallet watchlist (data/smart_wallets.db), reloaded by _reload_watch
EXTEND: dict[str, float] = {}     # mint -> follow-until (epoch s) after a watched wallet buys
WATCH_FOLLOW_S = 1800
_WATCH_AT = [0.0]


OPEN_FOLLOW_S = int(os.getenv("NOEMA_STREAM_OPEN_FOLLOW_S", "600"))
_OPEN_AT = [0.0]


def reload_open_positions(path: str | None = None, now: float | None = None) -> int:
    """Keep following trades for mints the paper META desk holds, so its exits mark on live trade prices
    instead of a stale observation tick. Read-only on the META db; missing db/table -> 0."""
    path = path or os.getenv("NOEMA_META_DB", "data/meta_launch.db")
    if not os.path.exists(path):
        return 0
    now = now or time.time()
    try:
        m = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            mints = [r[0] for r in m.execute("SELECT DISTINCT mint FROM meta_positions WHERE status='open'")]
        finally:
            m.close()
    except sqlite3.Error:
        return 0
    for mint in mints:
        EXTEND[mint] = max(EXTEND.get(mint, 0), now + OPEN_FOLLOW_S)
    return len(mints)


def safe_commit(c: sqlite3.Connection) -> bool:
    """Commit, but never let a busy/locked noema.db kill the stream: the txn stays open and the next commit retries."""
    try:
        c.commit()
        return True
    except sqlite3.OperationalError as exc:
        if not is_busy(exc):
            raise
        print(_now(), "commit deferred: database busy", flush=True)
        return False


async def committer(c: sqlite3.Connection, stop: asyncio.Event, every: float | None = None) -> None:
    """Commit on a clock, not only when a message arrives: a quiet/stalled websocket used to leave the shared
    connection's write txn open for the full 60 s recv timeout (and longer across reconnect backoff),
    holding the noema.db write lock against every other writer."""
    every = COMMIT_S if every is None else every
    last_alive = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(every)
        if c.in_transaction:
            safe_commit(c)
        if time.monotonic() - last_alive >= ALIVE_S:     # agent:launch_stream heartbeat = log mtime;
            last_alive = time.monotonic()                # a healthy-but-quiet stream used to read red
            print(_now(), "alive", flush=True)


def reload_watch(path: str = "data/smart_wallets.db") -> int:
    from .smart_wallets import load_watch
    w = load_watch(path)
    WATCH.clear()
    WATCH.update(w)
    now = time.time()
    for m in [m for m, u in EXTEND.items() if u < now]:
        EXTEND.pop(m, None)
    return len(WATCH)


def handle_rpc_logs(c: sqlite3.Connection, sig: str | None, logs: list[str], follow: dict[str, float] | None = None,
                    now: float | None = None) -> int:
    """Insert decoded trades for followed (recently created) mints; returns trades inserted."""
    follow = FOLLOW if follow is None else follow
    now = now or time.time()
    n = 0
    for ln in logs:
        if not ln.startswith("Program data: "):
            continue
        try:
            ev = decode_event(base64.b64decode(ln[14:]))
        except ValueError:
            continue
        if not ev:
            continue
        if ev["kind"] == "create":
            follow.setdefault(ev["mint"], now)
        elif sig and (now - follow.get(ev["mint"], -1e18) <= TRADE_FOLLOW_S or ev["trader"] in WATCH
                      or EXTEND.get(ev["mint"], 0) >= now):
            if ev["trader"] in WATCH and ev["side"] == "buy":
                EXTEND[ev["mint"]] = now + WATCH_FOLLOW_S  # price the watched wallet's exit (smart-wallet study)
            key = sig if n == 0 else f"{sig}:{n}"
            n += insert_trade(c, key, ev["mint"], ev["side"], ev["sol"], ev["tokens"], ev["trader"],
                              ev["market_cap_sol"], "helius")
    return n


def decode_launchlab(data: bytes) -> dict[str, Any] | None:
    """Raydium LaunchLab TradeEvent (147B): pool, total_base_sell, v_base, v_quote, real_base/quote before+after, ...,
    trade_direction @144 (0=buy). PoolCreateEvent: pool_state first. Quote assumed SOL (non-WSOL pools never followed)."""
    if data[:8] == _LL_CREATE and len(data) >= 40:
        return {"kind": "ll_create", "pool": b58(data[8:40])}
    if data[:8] != _LL_TRADE or len(data) != 147:
        return None
    u = [int.from_bytes(data[40 + 8 * i:48 + 8 * i], "little") for i in range(7)]
    _tbs, vb, vq, rbb, rqb, rba, rqa = u
    denom = vb - rba
    return {"kind": "ll_trade", "pool": b58(data[8:40]), "side": "sell" if data[144] else "buy",
            "sol": abs(rqa - rqb) / 1e9, "tokens": abs(rba - rbb) / 1e6,
            "market_cap_sol": round((vq + rqa) * 1e6 / denom, 4) if denom > 0 else None}


def handle_launchlab_logs(c: sqlite3.Connection, sig: str | None, logs: list[str], pools: dict | None = None,
                          now: float | None = None) -> tuple[int, list[str]]:
    """Returns (trades inserted, new pool_states needing a mint lookup)."""
    pools = LL_POOLS if pools is None else pools
    now = now or time.time()
    n, new = 0, []
    for ln in logs:
        if not ln.startswith("Program data: "):
            continue
        try:
            ev = decode_launchlab(base64.b64decode(ln[14:]))
        except ValueError:
            continue
        if not ev:
            continue
        if ev["kind"] == "ll_create":
            if ev["pool"] not in pools:
                pools[ev["pool"]] = {"mint": None, "at": now, "sig": sig}
                new.append(ev["pool"])
            continue
        p = pools.get(ev["pool"])
        if sig and p and p.get("mint") and now - p["at"] <= TRADE_FOLLOW_S:
            n += insert_trade(c, sig if n == 0 else f"{sig}:{n}", p["mint"], ev["side"], ev["sol"], ev["tokens"],
                              None, ev["market_cap_sol"], "helius_launchlab")
    return n, new


def mint_from_tx(tx: dict[str, Any]) -> tuple[str | None, bool]:
    """(base mint, quote_is_sol) from a LaunchLab create tx's token balances."""
    mints = {b.get("mint") for k in ("postTokenBalances", "preTokenBalances")
             for b in ((tx.get("meta") or {}).get(k) or [])} - {None}
    base = sorted(m for m in mints if m != WSOL)
    return (base[0] if len(base) == 1 else None), (WSOL in mints or len(base) == 1)


def _get_tx(sig: str) -> dict[str, Any] | None:
    import urllib.request
    url = rpc_http_url()
    if not url:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                       "params": [sig, {"maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read()).get("result")


async def resolve_ll_pools(c: sqlite3.Connection, q: asyncio.Queue, stop: asyncio.Event) -> None:
    """One getTransaction per LaunchLab create (a few/min), >=1s apart, retries w/ backoff - free-tier friendly."""
    while not stop.is_set():
        pool = await q.get()
        p = LL_POOLS.get(pool)
        if not p or not p.get("sig"):
            continue
        for attempt in range(4):
            await asyncio.sleep(2 + 3 * attempt)  # confirmed tx needs a moment; also rate-limits
            try:
                tx = await asyncio.to_thread(_get_tx, p["sig"])
            except Exception as exc:  # noqa: BLE001
                print(_now(), "rpc_http", type(exc).__name__, flush=True)  # never print the URL
                continue
            if tx:
                mint, sol_q = mint_from_tx(tx)
                if mint and sol_q:
                    p["mint"] = mint
                    c.execute("INSERT OR IGNORE INTO launch_stream (mint, signature, received_at, source, raw_json)"
                              " VALUES (?,?,?,?,?)", (mint, p["sig"], _now(), "helius_launchlab",
                                                      json.dumps({"pool": pool})))
                break
        await asyncio.sleep(1)


def prune_follow(follow: dict[str, float], now: float | None = None) -> None:
    now = now or time.time()
    for m in [m for m, t in follow.items() if now - t > TRADE_FOLLOW_S]:
        follow.pop(m, None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS launch_stream (mint TEXT PRIMARY KEY, signature TEXT, received_at TEXT NOT NULL,
  source TEXT NOT NULL, name TEXT, symbol TEXT, creator TEXT, initial_sol REAL, market_cap_sol REAL, raw_json TEXT);
CREATE INDEX IF NOT EXISTS launch_stream_at ON launch_stream(received_at);
CREATE TABLE IF NOT EXISTS launch_trades (signature TEXT PRIMARY KEY, mint TEXT NOT NULL, received_at TEXT NOT NULL,
  side TEXT, sol REAL, tokens REAL, trader TEXT, market_cap_sol REAL);
CREATE INDEX IF NOT EXISTS launch_trades_mint ON launch_trades(mint, received_at);
CREATE TABLE IF NOT EXISTS launch_trade_seen (signature TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL,
  PRIMARY KEY (signature, source));
CREATE TABLE IF NOT EXISTS launch_rpc_events (signature TEXT PRIMARY KEY, program TEXT, received_at TEXT NOT NULL,
  slot INTEGER);
"""


def rpc_ws_url() -> str:
    key = os.getenv("HELIUS_API_KEY")
    if key:
        return f"wss://mainnet.helius-rpc.com/?api-key={key}"
    return os.getenv("NOEMA_SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com")


def connect(path: str) -> sqlite3.Connection:
    c = sqlite_connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    if "source" not in {r[1] for r in c.execute("PRAGMA table_info(launch_trades)")}:
        c.execute("ALTER TABLE launch_trades ADD COLUMN source TEXT")
    return c


def pumpportal_url() -> str:
    key = os.getenv("PUMPPORTAL_API_KEY")  # never logged
    return f"{PUMPPORTAL}?api-key={key}" if key else PUMPPORTAL


def rpc_http_url() -> str | None:
    key = os.getenv("HELIUS_API_KEY")
    return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else os.getenv("NOEMA_SOLANA_HTTP_URL")


def insert_trade(c: sqlite3.Connection, sig: str, mint: str, side: str | None, sol: Any, tokens: Any, trader: Any,
                 mcap: Any, source: str) -> int:
    """Dedupe by signature across sources; launch_trade_seen records every source's arrival for overlap/latency."""
    now = _now()
    c.execute("INSERT OR IGNORE INTO launch_trade_seen VALUES (?,?,?)", (sig.split(":")[0], source, now))
    return c.execute("INSERT OR IGNORE INTO launch_trades (signature, mint, received_at, side, sol, tokens, trader,"
                     " market_cap_sol, source) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sig, mint, now, side, sol, tokens, trader, mcap, source)).rowcount


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
        FOLLOW.setdefault(msg["mint"], time.time())
        return msg["mint"]
    if tx in ("buy", "sell") and msg.get("mint") and msg.get("signature"):
        insert_trade(c, msg["signature"], msg["mint"], tx, msg.get("solAmount"), msg.get("tokenAmount"),
                     msg.get("traderPublicKey"), msg.get("marketCapSol"), "pumpportal")
    return None


class PumpPortalUnfunded(RuntimeError):
    """PumpPortal refused keyed trade data (key wallet below its minimum); reconnect later to re-check funding."""


def _is_funding_error(text: str) -> bool:
    t = text.lower()
    return "funded with at least" in t or "minimum balance not met" in t


def _pp_beat(ok: bool, err: str | None = None) -> None:
    try:
        from .heartbeats import beat
        beat("wallet:pumpportal_key", ok, err, cadence_s=300)
    except Exception:  # noqa: BLE001, S110 - heartbeat must never break the stream
        pass


async def run_pumpportal(c: sqlite3.Connection, stop: asyncio.Event) -> None:
    import websockets
    follow: dict[str, float] = {}
    backoff, fund_backoff = 1.0, 30.0
    while not stop.is_set():
        try:
            async with websockets.connect(pumpportal_url(), ping_interval=20, max_size=2**22) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))  # free, unmetered
                if not FOLLOW_TRADES:
                    _pp_beat(True, None)
                backoff, last_commit, fund_errs = 1.0, time.time(), 0
                print(_now(), "pumpportal connected", flush=True)
                while not stop.is_set():
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    if msg.get("txType") in ("buy", "sell"):
                        if fund_backoff > 30.0:
                            print(_now(), "pumpportal keyed trades accepted", flush=True)
                            _pp_beat(True)
                        fund_backoff, fund_errs = 30.0, 0
                    if "txType" not in msg and msg.get("message") or msg.get("errors"):
                        text = str(msg.get("message") or msg.get("errors"))
                        if _is_funding_error(text):
                            fund_errs += 1
                            if fund_errs >= 3 and FOLLOW_TRADES:
                                raise PumpPortalUnfunded(text[:160])
                        else:
                            print(_now(), "pumpportal msg", text[:160], flush=True)
                    new = handle_pumpportal(c, msg, follow)
                    if FOLLOW_TRADES and new and len(follow) <= MAX_FOLLOW:
                        await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [new]}))
                    old = [m for m, t in follow.items() if time.time() - t > TRADE_FOLLOW_S]
                    if old and FOLLOW_TRADES:
                        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": old}))
                        for m in old:
                            follow.pop(m, None)
                    if time.time() - last_commit > COMMIT_S:
                        safe_commit(c)
                        last_commit = time.time()
        except PumpPortalUnfunded as exc:
            # a fresh connection re-checks the key's funding; back off 30 s -> 10 min so we don't hammer it
            print(_now(), "pumpportal unfunded", str(exc)[:120], f"retry in {fund_backoff:.0f}s", flush=True)
            _pp_beat(False, "PumpPortal key wallet below 0.02 SOL - keyed trades refused; top it up")
            safe_commit(c)
            follow.clear()
            await asyncio.sleep(fund_backoff)
            fund_backoff = min(600.0, fund_backoff * 2)
        except Exception as exc:  # noqa: BLE001 - reconnect with backoff
            print(_now(), "pumpportal", type(exc).__name__, str(exc)[:120], flush=True)
            safe_commit(c)
            follow.clear()
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)


async def run_rpc(c: sqlite3.Connection, stop: asyncio.Event, llq: asyncio.Queue | None = None) -> None:
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
                    if RPC_TRADES and v.get("err") is None:
                        if subs.get(p.get("subscription")) == "raydium_launchlab":
                            _n, new_pools = handle_launchlab_logs(c, v.get("signature"), logs)
                            for pool in new_pools:
                                if llq is not None and llq.qsize() < 50:
                                    llq.put_nowait(pool)
                        else:
                            handle_rpc_logs(c, v.get("signature"), logs)
                    if time.time() - last_commit > COMMIT_S:
                        safe_commit(c)
                        prune_follow(FOLLOW)
                        if time.time() - _OPEN_AT[0] > 60:
                            _OPEN_AT[0] = time.time()
                            try:
                                reload_open_positions()
                            except Exception as exc:  # noqa: BLE001 - optional
                                print("open-position reload failed", type(exc).__name__, flush=True)
                        if time.time() - _WATCH_AT[0] > 300:
                            _WATCH_AT[0] = time.time()
                            try:
                                reload_watch()
                            except Exception as exc:  # noqa: BLE001 - watchlist is optional
                                print("watchlist reload failed", type(exc).__name__, flush=True)
                        for pk in [k for k, x in LL_POOLS.items() if time.time() - x["at"] > TRADE_FOLLOW_S]:
                            LL_POOLS.pop(pk, None)
                        last_commit = time.time()
        except Exception as exc:  # noqa: BLE001
            print(_now(), "rpc", type(exc).__name__, flush=True)  # never print the URL (may hold a key)
            safe_commit(c)
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


PRUNE_TABLES = ("launch_trades", "launch_rpc_events", "launch_trade_seen")


def prune_batch(c: sqlite3.Connection, table: str, cut: str, batch: int = 5000) -> int:
    assert table in PRUNE_TABLES
    n = c.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE received_at < ? LIMIT ?)",
                  (cut, batch)).rowcount
    safe_commit(c)
    return n


async def prune(c: sqlite3.Connection, keep_h: float = 72, batch: int = 5000) -> int:
    """Delete old rows on the stream's OWN connection in short batches. The old version opened a second
    connection while the shared one held a write txn -> it waited on itself for busy_timeout, raised
    'database is locked' and took the whole stream down (crash loop every ~hour)."""
    safe_commit(c)
    cut = datetime.fromtimestamp(time.time() - keep_h * 3600, UTC).isoformat()
    total = 0
    for t in PRUNE_TABLES:
        while True:
            n = prune_batch(c, t, cut, batch)
            total += n
            if n < batch:
                break
            await asyncio.sleep(0.05)  # let the stream tasks run between batches
    return total


def backfill_legacy_source(c: sqlite3.Connection) -> int:
    """Rows written before #177 added launch_trades.source have NULL source. The only trade writer that
    produced 4-dp market_cap_sol with a trader was the Helius pump.fun TradeEvent decoder (PumpPortal
    caps are unrounded floats; LaunchLab rows carry no trader), so those rows are 'helius'."""
    n = c.execute("UPDATE launch_trades SET source='helius' WHERE source IS NULL AND trader IS NOT NULL"
                  " AND market_cap_sol IS NOT NULL AND market_cap_sol = round(market_cap_sol, 4)").rowcount
    c.commit()
    return n


async def _main(db: str) -> None:
    stop = asyncio.Event()

    c = connect(db)  # one connection shared by all tasks (single thread); never open a second writer here

    async def housekeeping() -> None:
        while True:
            await asyncio.sleep(3600)
            try:  # housekeeping must never take the stream down
                print(_now(), "pruned", await prune(c), flush=True)
                print(_now(), json.dumps(await asyncio.to_thread(latency_report, db, 1)), flush=True)
            except Exception as exc:  # noqa: BLE001
                print(_now(), "housekeeping", type(exc).__name__, str(exc)[:120], flush=True)
    llq: asyncio.Queue = asyncio.Queue()
    await asyncio.gather(run_pumpportal(c, stop), run_rpc(c, stop, llq), resolve_ll_pools(c, llq, stop),
                         committer(c, stop), housekeeping())


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--backfill-source", action="store_true")
    a = ap.parse_args()
    if a.backfill_source:
        print("backfilled", backfill_legacy_source(connect(a.db)))
        return
    if a.report:
        print(json.dumps(latency_report(a.db), indent=1))
        return
    asyncio.run(_main(a.db))


if __name__ == "__main__":  # pragma: no cover
    main()
