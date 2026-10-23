"""Post-graduation PumpSwap pool tape (PAPER / READ ONLY, near-zero cost).

What: when a pump.fun coin graduates (bonding curve -> PumpSwap AMM pool), record the migration (mint, pool, slot,
time) and follow that pool's swaps for FOLLOW_S (default 60 min). Feeds MQGB's "be the house" backtests
(model-gated one-sided LP, post-graduation fades). Nothing here signs, trades or touches a wallet.

Cost design ("basically free"):
  * Migrations: Solana RPC `logsSubscribe` mentioning the pump.fun migration authority on the FREE public
    websocket (~100 txs/h). The PumpSwap CreatePoolEvent in those logs gives mint + pool. pump.fun's own
    CompletePumpAmmMigrationEvent is usually cut off ("Log truncated"), so it is not relied on.
    Gap backfill: getSignaturesForAddress + getTransaction on the free public HTTP RPC (capped per day).
  * Trades: one `logsSubscribe` per followed pool (mentions [pool]) on the same free connection, decoding the
    PumpSwap Anchor BuyEvent / SellEvent from "Program data:" logs - no getTransaction per trade.
  * Helius: gap-filler only. Used when every free websocket is down, for the newest HELIUS_MAX_POOLS pools,
    metered like Helius bills websockets (2 credits / 0.1 MB) and auto-stopped at HELIUS_CREDITS_DAY.
  * Pool head: the first trades can land before the subscription opens; ~20 s after a migration the pool's
    oldest signatures are read once over free HTTP (<= HEAD_MAX_TX getTransaction) to fill that gap.
  * Free sources also have a daily message / byte budget with auto-stop (be a polite public-RPC citizen).
  * Hot pools (bot-spam pools streaming > HOT_MB_MIN MB/min, ~60-70% of it FAILED txs that logsSubscribe cannot
    filter server-side) are duty-cycled: subscribed HOT_ON_S, then paused in proportion to how far over the
    rate they are (<= HOT_MAX_OFF_S). Every subscribed window is written to pool_coverage, so gaps are explicit.
  * Tail follow (TAIL_S, default 24 h): after the live window, each pool is polled over free HTTP:
    getSignaturesForAddress(until=last seen sig) -> failed txs skipped for free -> getTransaction only for new
    successful txs (<= TAIL_TX_PER_POLL per poll, evenly sampled incl. oldest+newest), same decoder, same rows,
    source 'tail_poll'. Active pools every TAIL_ACTIVE_S (5 min), dead pools (no successful tx in 2 h) every
    TAIL_DEAD_S (30 min). Own daily cap TAIL_HTTP_DAY, paced across the UTC day; Helius only as a capped
    fallback inside the same HELIUS_CREDITS_DAY.
Writes go to their own DB (data/pool_trades.db) through launch_stream.QueuedWriter: one background writer
thread, INSERT OR IGNORE, busy-retry - zero contention with noema.db and never blocks the event loop.
Status (usage vs budget) -> data/pool_stream_status.json, surfaced as heartbeat feed:pool_trades.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

from .launch_stream import WSOL, QueuedWriter, b58
from .sqlite_util import connect as sqlite_connect

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MIGRATION_AUTH = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"  # pump.fun migration authority (in every migrate tx)
SOL_QUOTES = {WSOL, "11111111111111111111111111111111"}

FOLLOW_S = int(os.getenv("NOEMA_POOL_FOLLOW_S", "3600"))            # follow window per graduated pool
MAX_POOLS = int(os.getenv("NOEMA_POOL_MAX", "80"))                  # concurrent pool subscriptions (newest first)
KEEP_D = float(os.getenv("NOEMA_POOL_KEEP_D", "14"))                # pool_trades retention (migrations kept)
FREE_MSGS_DAY = int(os.getenv("NOEMA_POOL_FREE_MSGS_DAY", "20000000"))
FREE_MB_DAY = float(os.getenv("NOEMA_POOL_FREE_MB_DAY", "40000"))   # public RPC politeness cap (~0.46 MB/s avg)
FREE_HTTP_DAY = int(os.getenv("NOEMA_POOL_FREE_HTTP_DAY", "30000"))  # backfill getSignatures/getTransaction calls
HELIUS_ON = os.getenv("NOEMA_POOL_HELIUS", "1") == "1"
HELIUS_CREDITS_DAY = float(os.getenv("NOEMA_POOL_HELIUS_CREDITS_DAY", "3000"))  # hard daily cap (~150 MB of ws)
HELIUS_MAX_POOLS = int(os.getenv("NOEMA_POOL_HELIUS_MAX_POOLS", "10"))
TAIL_ON = os.getenv("NOEMA_POOL_TAIL", "1") == "1"
TAIL_S = int(os.getenv("NOEMA_POOL_TAIL_S", "86400"))                # tail-follow horizon after migration
TAIL_ACTIVE_S = float(os.getenv("NOEMA_POOL_TAIL_ACTIVE_S", "300"))
TAIL_DEAD_S = float(os.getenv("NOEMA_POOL_TAIL_DEAD_S", "1800"))
TAIL_DEAD_AFTER_S = 7200.0   # no successful tx for 2 h -> dead cadence
TAIL_HTTP_DAY = int(os.getenv("NOEMA_POOL_TAIL_HTTP_DAY", "80000"))   # hard daily cap, tail polling only
TAIL_GAP_S = 1.0             # >= 1 s between tail calls (public RPC per-IP limits; also bounds the day at 86.4k)
TAIL_TX_PER_POLL = int(os.getenv("NOEMA_POOL_TAIL_TX_PER_POLL", "8"))
HELIUS_CREDITS_PER_CALL = 10  # conservative (archival calls); counted against HELIUS_CREDITS_DAY
HOT_MB_MIN = float(os.getenv("NOEMA_POOL_HOT_MB_MIN", "3"))         # live pool above this MB/min -> duty-cycled
HOT_ON_S = 20.0
HOT_MAX_OFF_S = float(os.getenv("NOEMA_POOL_HOT_MAX_OFF_S", "180"))
HELIUS_SESSION_S = 300.0     # on Helius, retry the free endpoints every 5 min
BACKFILL_S = float(os.getenv("NOEMA_POOL_BACKFILL_S", "120"))
STATUS_S = 30.0
FREE_WS = [u for u in os.getenv("NOEMA_POOL_WS_URLS", "wss://api.mainnet-beta.solana.com,"
                                "wss://solana-rpc.publicnode.com").split(",") if u]
FREE_HTTP = [u for u in os.getenv("NOEMA_POOL_HTTP_URLS", "https://api.mainnet-beta.solana.com,"
                                  "https://solana-rpc.publicnode.com").split(",") if u]
UA = {"Content-Type": "application/json", "User-Agent": "noema-pool-stream/1 (paper research)"}

_D = lambda n: hashlib.sha256(f"event:{n}".encode()).digest()[:8]
CREATE_POOL, BUY, SELL = _D("CreatePoolEvent"), _D("BuyEvent"), _D("SellEvent")
# Execution vs constant-product quote from the event's own (pre-trade) reserves. Graduated PumpSwap pools fill
# above raw x*y=k on BOTH sides, as if ~15-18 SOL of extra quote sat in the curve (premium ~7% at 260 SOL, ~50% at
# 35 SOL). So this is only a decode-garbage guard: flag when exec / expected falls outside [1/2, 4].
# Verified on-chain 2026-10-22 (txs 4KVi49Gw.., 3oCes3a7.., KykDmPLX..): decoded fills match
# vault token/SOL deltas exactly; PumpSwap fills sit 7-50% above raw-vault x*y=k because
# pricing uses extra virtual quote reserve, so decoded price is right, raw reserve price is low.
OFF_RESERVES_BAND = (0.5, 4.0)
MIG_QUOTE_SOL = (50.0, 200.0)  # a standard pump.fun graduation seeds ~85 SOL; outside this -> "nonstandard"
CHECK_MIN_SOL = 0.001        # tiny trades: integer rounding dominates, skip the reserve check
LOW_LIQ_SOL = 1.0            # pool quote reserve below this -> prices are not meaningful


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _u64(d: bytes, o: int) -> int:
    return int.from_bytes(d[o:o + 8], "little")


# ---- decoders ------------------------------------------------------------------------------------
def decode_create_pool(d: bytes) -> dict[str, Any] | None:
    """PumpSwap CreatePoolEvent: ts i64, index u16, creator, base_mint, quote_mint, base/quote decimals u8,
    7 x u64 (base_in, quote_in, pool_base, pool_quote, min_liq, initial_liq, lp_out), pool_bump u8, pool, lp_mint."""
    if d[:8] != CREATE_POOL or len(d) < 237:
        return None
    u = [_u64(d, 116 + 8 * i) for i in range(7)]
    return {"kind": "create_pool", "ts": _u64(d, 8), "creator": b58(d[18:50]), "mint": b58(d[50:82]),
            "quote_mint": b58(d[82:114]), "base_dec": d[114], "quote_dec": d[115], "pool_base": u[2],
            "pool_quote": u[3], "pool": b58(d[173:205]), "lp_mint": b58(d[205:237])}


def decode_swap(d: bytes) -> dict[str, Any] | None:
    """PumpSwap BuyEvent / SellEvent: ts i64 then 13 u64 (base, limit, user_base, user_quote, pool_base, pool_quote,
    quote [ex-fee], lp_fee_bps, lp_fee, protocol_fee_bps, protocol_fee, quote_w_lp_fee, user_quote [w/ all fees]),
    pool, user. Pool reserves are the PRE-trade reserves."""
    side = "buy" if d[:8] == BUY else "sell" if d[:8] == SELL else None
    if side is None or len(d) < 184:
        return None
    u = [_u64(d, 16 + 8 * i) for i in range(13)]
    return {"kind": "swap", "side": side, "ts": _u64(d, 8), "base": u[0], "pool_base": u[4], "pool_quote": u[5],
            "quote": u[6], "lp_fee": u[8], "protocol_fee": u[10], "user_quote": u[12],
            "pool": b58(d[120:152]), "trader": b58(d[152:184])}


def program_data(logs: list[str]) -> list[bytes]:
    out = []
    for ln in logs:
        if ln.startswith("Program data: "):
            try:
                out.append(base64.b64decode(ln[14:]))
            except ValueError:
                continue
    return out


def migration_from_logs(logs: list[str]) -> list[dict[str, Any]]:
    """CreatePoolEvents of a pump.fun migrate tx (Migrate / MigrateV2 instruction)."""
    if not any("Instruction: Migrate" in ln for ln in logs):
        return []
    return [e for e in map(decode_create_pool, program_data(logs)) if e]


def quality(ev: dict[str, Any], quote_mint: str | None = WSOL) -> str | None:
    """Ingest sanity flags (kept, flagged - loaders exclude), in the spirit of tape_quality.row_reason:
    zero_sol, non_sol_quote, off_reserves (execution vs the event's own constant-product reserves outside [1/2, 4]),
    low_liq (pool quote reserve < 1 SOL)."""
    r = []
    if quote_mint not in SOL_QUOTES:
        r.append("non_sol_quote")
    b, q, B, Q = ev["base"], ev["quote"], ev["pool_base"], ev["pool_quote"]
    if b <= 0 or q <= 0:
        r.append("zero_sol")
    elif q / 1e9 >= CHECK_MIN_SOL and B > 0 and Q > 0:
        denom = B - b if ev["side"] == "buy" else B + b
        exp = Q * b / denom if denom > 0 else float("inf")
        if exp <= 0 or not OFF_RESERVES_BAND[0] <= q / exp <= OFF_RESERVES_BAND[1]:
            r.append("off_reserves")
    if Q / 1e9 < LOW_LIQ_SOL:
        r.append("low_liq")
    return ",".join(r) or None


def trade_row(ev: dict[str, Any], sig: str, n: int, mint: str, slot: int | None, source: str,
              quote_mint: str | None = WSOL, received_at: str | None = None) -> tuple:
    sol, tok = ev["quote"] / 1e9, ev["base"] / 1e6   # PumpSwap graduates: base 6 decimals, quote WSOL 9
    B, Q = ev["pool_base"], ev["pool_quote"]          # pre-trade reserves (as emitted by the event)
    price = (sol / tok) if tok > 0 else None
    mcap = round(price * 1e9, 4) if price else None   # execution price x 1B supply, same basis as the curve tape
    return (sig, ev["pool"], n, mint, "pump_amm", slot, ev["ts"], received_at or _now(), ev["side"], sol, tok,
            ev["trader"], price, mcap, B / 1e6, Q / 1e9, ev["lp_fee"] / 1e9,
            ev["protocol_fee"] / 1e9, source, quality(ev, quote_mint))


# ---- storage -------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_migrations (pool TEXT PRIMARY KEY, mint TEXT NOT NULL, signature TEXT, slot INTEGER,
  block_ts INTEGER, received_at TEXT NOT NULL, source TEXT NOT NULL, program TEXT, quote_mint TEXT,
  pool_base REAL, pool_quote_sol REAL, creator TEXT, lp_mint TEXT, quality TEXT);
CREATE INDEX IF NOT EXISTS pool_migrations_mint ON pool_migrations(mint);
CREATE INDEX IF NOT EXISTS pool_migrations_ts ON pool_migrations(block_ts);
CREATE TABLE IF NOT EXISTS pool_trades (signature TEXT NOT NULL, pool TEXT NOT NULL, n INTEGER NOT NULL,
  mint TEXT NOT NULL, program TEXT NOT NULL, slot INTEGER, block_ts INTEGER, received_at TEXT NOT NULL,
  side TEXT NOT NULL, sol REAL, tokens REAL, trader TEXT, price REAL, mcap_sol REAL, pool_base REAL,
  pool_quote_sol REAL, lp_fee_sol REAL, protocol_fee_sol REAL, source TEXT, quality TEXT,
  PRIMARY KEY (signature, pool, n));
CREATE INDEX IF NOT EXISTS pool_trades_pool ON pool_trades(pool, slot);
CREATE INDEX IF NOT EXISTS pool_trades_at ON pool_trades(received_at);
CREATE TABLE IF NOT EXISTS pool_coverage (pool TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL,
  source TEXT NOT NULL, complete INTEGER, sigs_ok INTEGER, txs INTEGER, PRIMARY KEY (pool, start_ts, source));
CREATE INDEX IF NOT EXISTS pool_coverage_end ON pool_coverage(end_ts);
CREATE TABLE IF NOT EXISTS pool_stream_usage (day TEXT NOT NULL, source TEXT NOT NULL, msgs INTEGER, bytes INTEGER,
  http_calls INTEGER, credits REAL, updated_at TEXT, PRIMARY KEY (day, source));
"""
INSERT_TRADE = "INSERT OR IGNORE INTO pool_trades VALUES (" + ",".join("?" * 20) + ")"
INSERT_COV = "INSERT OR REPLACE INTO pool_coverage VALUES (?,?,?,?,?,?,?)"
INSERT_MIG = ("INSERT OR IGNORE INTO pool_migrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def migration_quality(quote_mint: str | None, quote_sol: float | None) -> str | None:
    """non_sol_quote (USDC / token-paired curves - not followed), nonstandard (seed SOL far from ~85)."""
    if quote_mint not in SOL_QUOTES:
        return "non_sol_quote"
    if quote_sol is None or not MIG_QUOTE_SOL[0] <= quote_sol <= MIG_QUOTE_SOL[1]:
        return "nonstandard"
    return None


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite_connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


def default_db() -> str:
    return os.getenv("NOEMA_POOL_DB", os.path.join(os.path.dirname(os.getenv("NOEMA_DB_PATH", "data/noema.db")),
                                                   "pool_trades.db"))


def status_path(db: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db)), "pool_stream_status.json")


# ---- budget --------------------------------------------------------------------------------------
def utc_day(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or time.time(), UTC).strftime("%Y-%m-%d")


def helius_credits(nbytes: int, http_calls: int = 0) -> float:
    """Helius bills all websocket traffic at 2 credits per 0.1 MB (uncompressed) and 1 credit per standard RPC call."""
    return nbytes / 100_000 * 2 + http_calls


class Budget:
    """Per-UTC-day usage per source ('free' public RPC, 'helius'); auto-stop when a cap is reached."""

    def __init__(self, day_fn: Any = utc_day) -> None:
        self.day_fn, self.day = day_fn, day_fn()
        self.u: dict[str, dict[str, int]] = {}

    def _get(self, src: str) -> dict[str, int]:
        d = self.day_fn()
        if d != self.day:           # UTC rollover resets every counter (and lifts any auto-stop)
            self.day, self.u = d, {}
        return self.u.setdefault(src, {"msgs": 0, "bytes": 0, "http": 0})

    def add(self, src: str, nbytes: int = 0, msgs: int = 1, http: int = 0) -> None:
        g = self._get(src)
        g["msgs"] += msgs
        g["bytes"] += nbytes
        g["http"] += http

    def load(self, src: str, msgs: int, nbytes: int, http: int) -> None:
        g = self._get(src)
        g["msgs"], g["bytes"], g["http"] = max(g["msgs"], msgs), max(g["bytes"], nbytes), max(g["http"], http)

    def credits(self, src: str = "helius") -> float:
        g = self._get(src)
        return helius_credits(g["bytes"], g["http"])

    def helius_ok(self) -> bool:
        return HELIUS_ON and self.credits("helius") < HELIUS_CREDITS_DAY

    def free_ok(self) -> bool:
        g = self._get("free")
        return g["msgs"] < FREE_MSGS_DAY and g["bytes"] < FREE_MB_DAY * 1e6

    def free_http_ok(self) -> bool:
        return self._get("free")["http"] < FREE_HTTP_DAY

    def tail_ok(self) -> bool:
        return self._get("tail")["http"] < TAIL_HTTP_DAY

    def tail_paced(self, t: float | None = None) -> bool:
        """True when tail polling is ahead of an even spread of TAIL_HTTP_DAY over the UTC day (+30 min slack)."""
        t = t or time.time()
        frac = min(1.0, ((t % 86400) + 1800) / 86400)
        return self._get("tail")["http"] >= TAIL_HTTP_DAY * frac


# ---- state ---------------------------------------------------------------------------------------
class State:
    def __init__(self) -> None:
        self.pools: dict[str, dict[str, Any]] = {}   # pool -> {mint, at, quote_mint}
        self.seen_sigs: dict[str, float] = {}        # migration-authority sigs already handled
        self.budget = Budget()
        self.mode = "starting"
        self.stopped: str | None = None
        self.n = {"migrations": 0, "trades": 0, "flagged": 0, "dropped": 0, "tx_err": 0, "truncated_no_event": 0,
                  "backfilled": 0, "head_trades": 0, "tail_polls": 0, "tail_tx": 0, "tail_trades": 0,
                  "tail_gaps": 0, "hot_pauses": 0}
        self.free_fail = 0
        self.last_msg = 0.0

    def desired(self, cap: int, now: float | None = None) -> list[str]:
        now = now or time.time()
        live = [(v["at"], p) for p, v in self.pools.items()
                if now - v["at"] <= FOLLOW_S and v.get("quote_mint") in SOL_QUOTES and v.get("off_until", 0) <= now]
        return [p for _a, p in sorted(live, reverse=True)[:cap]]

    def tail_pools(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        return [p for p, v in self.pools.items() if TAIL_ON and v.get("quote_mint") in SOL_QUOTES
                and FOLLOW_S < now - v["at"] <= TAIL_S]

    def tail_active(self, p: str, now: float | None = None) -> bool:
        v = self.pools[p]
        return (now or time.time()) - (v.get("last_trade") or v["at"]) < TAIL_DEAD_AFTER_S

    def prune(self, now: float | None = None) -> None:
        now = now or time.time()
        keep = (TAIL_S if TAIL_ON else FOLLOW_S) + 600
        for p in [p for p, v in self.pools.items() if now - v["at"] > keep]:
            self.pools.pop(p, None)
        for s in [s for s, t in self.seen_sigs.items() if now - t > 6 * 3600]:
            self.seen_sigs.pop(s, None)


def register_migration(w: Any, st: State, ev: dict[str, Any], sig: str | None, slot: int | None, source: str,
                       now: float | None = None) -> bool:
    now = now or time.time()
    if sig:
        st.seen_sigs[sig] = now
    if ev["pool"] in st.pools:
        return False
    at = float(ev["ts"]) if ev.get("ts") and abs(now - ev["ts"]) < 86400 else now
    st.pools[ev["pool"]] = {"mint": ev["mint"], "at": min(at, now), "quote_mint": ev["quote_mint"], "seen": now}
    w.execute(INSERT_MIG, (ev["pool"], ev["mint"], sig, slot, ev.get("ts"), _now(), source, PUMP_AMM,
                           ev["quote_mint"], ev["pool_base"] / 1e6, ev["pool_quote"] / 1e9, ev.get("creator"),
                           ev.get("lp_mint"), migration_quality(ev["quote_mint"], ev["pool_quote"] / 1e9)))
    st.n["migrations"] += 1
    return True


def handle_pool_logs(w: Any, st: State, sig: str | None, logs: list[str], slot: int | None, source: str) -> int:
    """Insert every Buy/SellEvent in the tx that belongs to a followed pool. Returns rows queued."""
    if not sig:
        return 0
    per: dict[str, int] = {}
    k = 0
    for d in program_data(logs):
        ev = decode_swap(d)
        if not ev:
            continue
        p = st.pools.get(ev["pool"])
        if not p:
            continue
        if slot is not None and source.endswith("_ws") and p.get("first_slot") is None:
            p["first_slot"] = slot   # head backfill reads HTTP history up to here
        if slot is not None and slot >= p.get("last_slot", -1):
            p["last_slot"], p["last_sig"] = slot, sig   # tail-follow cursor
        if ev.get("ts"):
            p["last_trade"] = max(p.get("last_trade") or 0, float(ev["ts"]))
        n = per.get(ev["pool"], 0)
        per[ev["pool"]] = n + 1
        row = trade_row(ev, sig, n, p["mint"], slot, source, p.get("quote_mint"))
        if row[9] <= 0 or row[10] <= 0:   # sol / tokens: nothing to price
            st.n["dropped"] += 1
            continue
        w.execute(INSERT_TRADE, row)
        st.n["trades"] += 1
        st.n["flagged"] += row[-1] is not None
        k += 1
    if not k and any("truncated" in ln for ln in logs):
        st.n["truncated_no_event"] += 1
    return k


def hot_off_s(v: dict[str, Any], now: float) -> float:
    """Rolling 60 s byte meter of a subscribed pool. Over HOT_MB_MIN MB/min (measured over >= HOT_ON_S) -> pause
    for HOT_ON_S * (rate / HOT_MB_MIN - 1) s (<= HOT_MAX_OFF_S), so a hot pool averages ~HOT_MB_MIN MB/min."""
    age = now - v.get("w0", now)
    rate = v.get("wb", 0) / max(age, 1e-9) * 60 / 1e6
    if age >= HOT_ON_S and rate > HOT_MB_MIN:
        v["hot"], v["wb"], v["w0"] = True, 0, now
        return max(1.0, min(HOT_MAX_OFF_S, HOT_ON_S * (rate / HOT_MB_MIN - 1)))
    if age >= 60:                      # quiet minute: reset the window
        v["hot"], v["wb"], v["w0"] = False, 0, now
    return 0.0


# ---- network -------------------------------------------------------------------------------------
def helius_ws() -> str | None:
    key = os.getenv("HELIUS_API_KEY")
    return f"wss://mainnet.helius-rpc.com/?api-key={key}" if key else None   # never printed


def helius_http() -> str | None:
    key = os.getenv("HELIUS_API_KEY")
    return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else None   # never printed


def _rpc(url: str, method: str, params: list) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, body, UA), timeout=20) as r:
        return json.loads(r.read()).get("result")


async def session(url: str, src: str, w: Any, st: State, stop: asyncio.Event, cap: int,
                  until: float | None = None) -> None:
    """One websocket: migration-authority subscription + one per followed pool, reconciled every 2 s."""
    import websockets
    async with websockets.connect(url, ping_interval=20, max_size=2**23, open_timeout=15,
                                  user_agent_header=UA["User-Agent"]) as ws:
        rid = 0
        pending: dict[int, tuple[str, str | None]] = {}
        subs: dict[int, str] = {}            # subscription id -> "mig" | pool
        pool_sub: dict[str, int] = {}
        sub_at: dict[str, float] = {}         # pool -> wall time its subscription was confirmed

        def covered(p: str, end: float) -> None:
            t0 = sub_at.pop(p, None)
            if t0 is not None and end > t0:
                w.execute(INSERT_COV, (p, t0, end, f"{src}_ws", 1, None, None))

        async def send(method: str, params: list, tag: tuple[str, str | None]) -> None:
            nonlocal rid
            rid += 1
            pending[rid] = tag
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))

        await send("logsSubscribe", [{"mentions": [MIGRATION_AUTH]}, {"commitment": "confirmed"}], ("mig", None))
        st.mode, st.last_msg, last_rec, first = src, time.monotonic(), 0.0, True
        print(_now(), "connected", src, flush=True)
        try:
            while not stop.is_set():
                if until and time.time() > until:
                    return
                if time.monotonic() - last_rec >= 2:
                    last_rec = time.monotonic()
                    want = st.desired(cap)
                    asked = {t[1] for t in pending.values() if t[0] == "pool"}
                    for p in want:
                        if p not in pool_sub and p not in asked:
                            await send("logsSubscribe", [{"mentions": [p]}, {"commitment": "confirmed"}], ("pool", p))
                    now = time.time()
                    for p in list(pool_sub):          # duty-cycle bot-spam pools (bytes are mostly failed txs)
                        v = st.pools.get(p) or {}
                        off = hot_off_s(v, now)
                        if off:
                            v["off_until"] = now + off
                            st.n["hot_pauses"] += 1
                            want = [q for q in want if q != p]
                    for p in set(pool_sub) - set(want):
                        sid = pool_sub.pop(p)
                        subs.pop(sid, None)
                        covered(p, now)
                        await send("logsUnsubscribe", [sid], ("unsub", p))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except TimeoutError:
                    if time.monotonic() - st.last_msg > 90:
                        raise TimeoutError("no messages for 90 s") from None
                    continue
                st.last_msg = time.monotonic()
                st.budget.add(src, len(raw))
                if src == "helius" and not st.budget.helius_ok():
                    st.stopped = f"helius daily budget reached ({HELIUS_CREDITS_DAY:.0f} credits)"
                    return
                if src == "free" and not st.budget.free_ok():
                    st.stopped = "free-RPC daily message/byte budget reached"
                    return
                m = json.loads(raw)
                if "id" in m and m.get("id") in pending:
                    kind, p = pending.pop(m["id"])
                    if "error" in m:
                        print(_now(), "sub error", kind, str(m["error"])[:120], flush=True)
                    elif kind in ("mig", "pool") and isinstance(m.get("result"), int):
                        subs[m["result"]] = "mig" if kind == "mig" else p
                        if p:
                            pool_sub[p] = m["result"]
                            sub_at[p] = time.time()
                            if p in st.pools:
                                st.pools[p]["wb"], st.pools[p]["w0"] = 0, time.time()
                    if first and kind == "mig":
                        st.free_fail, first = (0 if src == "free" else st.free_fail), False
                    continue
                prm = m.get("params") or {}
                res = prm.get("result") or {}
                v = res.get("value") or {}
                key = subs.get(prm.get("subscription"))
                if key is None:
                    continue
                if key != "mig" and key in st.pools:
                    st.pools[key]["wb"] = st.pools[key].get("wb", 0) + len(raw)
                if v.get("err") is not None:
                    st.n["tx_err"] += 1
                    continue
                logs, slot, sig = v.get("logs") or [], (res.get("context") or {}).get("slot"), v.get("signature")
                if key == "mig":
                    if sig:
                        st.seen_sigs[sig] = time.time()
                    for ev in migration_from_logs(logs):
                        register_migration(w, st, ev, sig, slot, f"{src}_ws")
                else:
                    handle_pool_logs(w, st, sig, logs, slot, f"{src}_ws")
        finally:
            for p in list(sub_at):
                covered(p, time.time())


async def run_stream(w: Any, st: State, stop: asyncio.Event) -> None:
    i, backoff = 0, 2.0
    while not stop.is_set():
        if not st.budget.free_ok():
            st.mode = "paused"
            await asyncio.sleep(60)
            continue
        if st.stopped and (("free" in st.stopped and st.budget.free_ok())
                           or ("helius" in st.stopped and st.budget.helius_ok())):
            st.stopped = None             # new UTC day (budgets reset)
        use_helius = st.free_fail >= 2 * len(FREE_WS) and helius_ws() and st.budget.helius_ok()
        try:
            if use_helius:
                await session(helius_ws() or "", "helius", w, st, stop, min(MAX_POOLS, HELIUS_MAX_POOLS),
                              until=time.time() + HELIUS_SESSION_S)
                st.free_fail = 0          # go back and retry the free endpoints
            else:
                await session(FREE_WS[i % len(FREE_WS)], "free", w, st, stop, MAX_POOLS)
        except Exception as exc:  # noqa: BLE001 - reconnect; never print the URL (Helius one holds a key)
            print(_now(), "ws", "helius" if use_helius else f"free#{i % len(FREE_WS)}", type(exc).__name__,
                  str(exc)[:80] if not use_helius else "", flush=True)
            if not use_helius:
                st.free_fail += 1
                i += 1
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)
            continue
        backoff = 2.0


HEAD_DELAY_S = 20.0           # let the pool's first slots finalise before reading its head over HTTP
HEAD_PAGES = 5                # <= 5000 signatures back; busier pools keep a (flagged-by-absence) head gap
HEAD_MAX_TX = int(os.getenv("NOEMA_POOL_HEAD_MAX_TX", "15"))   # getTransaction cap per pool head


class Http:
    """Serialised, budgeted calls to the free public HTTP RPC (>= 0.6 s apart, rotate endpoint on error).
    src='free' (migration / head backfill, FREE_HTTP_DAY) or 'tail' (tail follow, TAIL_HTTP_DAY; when every free
    endpoint fails it may fall back to Helius, metered into the same HELIUS_CREDITS_DAY cap)."""

    def __init__(self, st: State, src: str = "free", helius_fallback: bool = False, gap_s: float = 0.6) -> None:
        self.st, self.j, self.src, self.helius_fallback, self.gap_s = st, 0, src, helius_fallback, gap_s

    def _ok(self) -> bool:
        return self.st.budget.tail_ok() if self.src == "tail" else self.st.budget.free_http_ok()

    async def call(self, method: str, params: list) -> Any:
        if not self._ok():
            raise RuntimeError(f"{self.src} http daily budget reached")
        err: Exception | None = None
        for _ in range(len(FREE_HTTP) if self.helius_fallback else 1):
            await asyncio.sleep(self.gap_s)
            self.st.budget.add(self.src, http=1, msgs=0)
            try:
                return await asyncio.to_thread(_rpc, FREE_HTTP[self.j % len(FREE_HTTP)], method, params)
            except Exception as exc:  # noqa: BLE001
                self.j += 1
                err = exc
        url = helius_http() if self.helius_fallback else None
        if url and HELIUS_ON and (self.st.budget.credits("helius") + HELIUS_CREDITS_PER_CALL
                                  <= HELIUS_CREDITS_DAY):
            self.st.budget.add("helius", http=HELIUS_CREDITS_PER_CALL, msgs=0)
            try:
                return await asyncio.to_thread(_rpc, url, method, params)
            except Exception:  # noqa: BLE001 - never surface the keyed URL
                raise RuntimeError("helius http fallback failed") from None
        raise err or RuntimeError("http failed")


async def backfill_migrations(http: Http, w: Any, st: State) -> int:
    """Catch migrations the websocket missed (reconnects, restarts)."""
    sigs = await http.call("getSignaturesForAddress", [MIGRATION_AUTH, {"limit": 200}])
    now, k = time.time(), 0
    todo = [x for x in sigs or [] if x.get("err") is None and x["signature"] not in st.seen_sigs
            and now - (x.get("blockTime") or now) <= FOLLOW_S]
    for x in todo[:40]:
        tx = await http.call("getTransaction", [x["signature"], {"maxSupportedTransactionVersion": 1,
                                                                 "commitment": "confirmed"}])
        if tx is None:
            continue
        st.seen_sigs[x["signature"]] = now
        for ev in migration_from_logs((tx.get("meta") or {}).get("logMessages") or []):
            if register_migration(w, st, ev, x["signature"], tx.get("slot"), "free_http"):
                st.n["backfilled"] += 1
                k += 1
    return k


async def backfill_head(http: Http, w: Any, st: State, pool: str) -> int:
    """The first trades of a pool land before (or right as) its websocket subscription opens. Read the pool's
    oldest signatures over HTTP and decode the trades the stream could not have seen (dedupe = primary key)."""
    p = st.pools.get(pool) or {}
    first, before, sigs = p.get("first_slot"), None, []
    for _ in range(HEAD_PAGES):   # newest-first pages; walk back to the pool's creation
        opts: dict[str, Any] = {"limit": 1000}
        if before:
            opts["before"] = before
        page = await http.call("getSignaturesForAddress", [pool, opts]) or []
        sigs += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    old = [x for x in reversed(sigs) if x.get("err") is None and (first is None or x["slot"] <= first)]
    n = 0
    for x in old[:HEAD_MAX_TX]:
        tx = await http.call("getTransaction", [x["signature"], {"maxSupportedTransactionVersion": 1,
                                                                 "commitment": "confirmed"}])
        if tx:
            n += handle_pool_logs(w, st, x["signature"], (tx.get("meta") or {}).get("logMessages") or [],
                                  tx.get("slot"), "free_http")
    st.n["head_trades"] = st.n.get("head_trades", 0) + n
    return n


async def run_backfill(w: Any, st: State, stop: asyncio.Event) -> None:
    http, last_mig = Http(st), 0.0
    while not stop.is_set():
        try:
            if time.time() - last_mig >= BACKFILL_S:
                last_mig = time.time()
                await backfill_migrations(http, w, st)
            now = time.time()
            due = [q for q, v in st.pools.items() if not v.get("head_done") and v.get("quote_mint") in SOL_QUOTES
                   and now - v["seen"] >= HEAD_DELAY_S and now - v["at"] <= FOLLOW_S]
            for q in sorted(due, key=lambda q: -st.pools[q]["at"])[:5]:
                st.pools[q]["head_done"] = True
                await backfill_head(http, w, st, q)
        except Exception as exc:  # noqa: BLE001
            print(_now(), "backfill", type(exc).__name__, str(exc)[:80], flush=True)
            await asyncio.sleep(10)
        await asyncio.sleep(2)


def sample_evenly(xs: list, k: int) -> list:
    """<= k items spread across xs, always keeping the first and last (price path endpoints)."""
    if len(xs) <= k:
        return list(xs)
    if k <= 1:
        return [xs[-1]]
    idx = sorted({round(i * (len(xs) - 1) / (k - 1)) for i in range(k)})
    return [xs[i] for i in idx]


async def tail_poll(http: Http, w: Any, st: State, pool: str, now: float | None = None) -> int:
    """One tail-follow poll: new signatures since the cursor, failed ones skipped (free - the signature list
    carries err), <= TAIL_TX_PER_POLL successful txs decoded. Writes a pool_coverage row; complete=1 only when
    every new successful tx was decoded (no page overflow, no sampling)."""
    p = st.pools[pool]
    opts: dict[str, Any] = {"limit": 1000}
    cursor = p.get("last_sig")
    if cursor:
        opts["until"] = cursor
    sigs = await http.call("getSignaturesForAddress", [pool, opts]) or []
    now = now or time.time()
    st.n["tail_polls"] += 1
    ok = [x for x in reversed(sigs) if x.get("err") is None]          # oldest first
    pick = sample_evenly(ok, TAIL_TX_PER_POLL)
    n, missed = 0, 0
    for x in pick:   # last_trade (activity) only moves on decoded swaps - fee sweeps etc. also succeed
        try:
            tx = await http.call("getTransaction", [x["signature"], {"maxSupportedTransactionVersion": 0,
                                                                     "commitment": "confirmed"}])
        except Exception:  # noqa: BLE001 - one bad tx never blocks the cursor; coverage marked incomplete
            tx = None
            if not st.budget.tail_ok():
                missed += len(pick)
                break
        st.n["tail_tx"] += 1
        if tx is None:
            missed += 1
        elif not (tx.get("meta") or {}).get("err"):
            n += handle_pool_logs(w, st, x["signature"], (tx.get("meta") or {}).get("logMessages") or [],
                                  tx.get("slot"), "tail_poll")
    if sigs:
        p["last_sig"], p["last_slot"] = sigs[0]["signature"], max(p.get("last_slot", -1), sigs[0].get("slot") or 0)
    complete = int(bool(cursor) and len(sigs) < 1000 and len(pick) == len(ok) and not missed)
    st.n["tail_gaps"] += 1 - complete
    w.execute(INSERT_COV, (pool, p.get("tail_from") or p["at"] + FOLLOW_S, now, "tail_poll", complete, len(ok),
                           len(pick)))
    p["tail_from"] = now
    st.n["tail_trades"] += n
    p["tail_next"] = now + (TAIL_ACTIVE_S if st.tail_active(pool, now) else TAIL_DEAD_S)
    return n


async def run_tail(w: Any, st: State, stop: asyncio.Event) -> None:
    """Tail-follow loop: due pools, active first then newest; paced across the UTC day under TAIL_HTTP_DAY."""
    http = Http(st, "tail", helius_fallback=True, gap_s=TAIL_GAP_S)
    while not stop.is_set() and TAIL_ON:
        now = time.time()
        due = [q for q in st.tail_pools(now) if st.pools[q].get("tail_next", 0) <= now]
        if not due or not st.budget.tail_ok() or st.budget.tail_paced(now):
            await asyncio.sleep(5)
            continue
        due.sort(key=lambda q: (not st.tail_active(q, now), -st.pools[q]["at"]))
        q = due[0]
        try:
            await tail_poll(http, w, st, q)
        except Exception as exc:  # noqa: BLE001
            st.pools[q]["tail_next"] = time.time() + 60
            print(_now(), "tail", type(exc).__name__, str(exc)[:80], flush=True)
            await asyncio.sleep(5)


def write_status(path: str, st: State, followed: int) -> dict[str, Any]:
    f, h, t = st.budget._get("free"), st.budget._get("helius"), st.budget._get("tail")
    tail = st.tail_pools()
    s = {"updated_at": _now(), "day_utc": st.budget.day, "mode": st.mode, "stopped": st.stopped,
         "follow_s": FOLLOW_S, "max_pools": MAX_POOLS, "followed": followed, "session": dict(st.n),
         "hot_pools": sum(1 for v in st.pools.values() if v.get("hot")),
         "tail": {"enabled": TAIL_ON, "tail_s": TAIL_S, "pools": len(tail),
                  "active": sum(1 for q in tail if st.tail_active(q)), "http_calls": t["http"],
                  "http_budget": TAIL_HTTP_DAY, "tx_per_poll": TAIL_TX_PER_POLL},
         "free": {"msgs": f["msgs"], "mb": round(f["bytes"] / 1e6, 1), "http_calls": f["http"],
                  "msgs_budget": FREE_MSGS_DAY, "mb_budget": FREE_MB_DAY, "http_budget": FREE_HTTP_DAY},
         "helius": {"enabled": HELIUS_ON and bool(os.getenv("HELIUS_API_KEY")), "mb": round(h["bytes"] / 1e6, 2),
                    "credits": round(st.budget.credits("helius"), 1), "credits_budget": HELIUS_CREDITS_DAY,
                    "max_pools": HELIUS_MAX_POOLS}}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".pss")
    with os.fdopen(fd, "w") as fh:
        json.dump(s, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return s


def _save_usage(st: State) -> Any:
    rows = [(st.budget.day, src, g["msgs"], g["bytes"], g["http"],
             helius_credits(g["bytes"], g["http"]) if src == "helius" else 0.0, _now())
            for src, g in list(st.budget.u.items())]

    def fn(c: sqlite3.Connection) -> int:
        c.executemany("INSERT OR REPLACE INTO pool_stream_usage VALUES (?,?,?,?,?,?,?)", rows)
        c.commit()
        return len(rows)
    return fn


def prune_sync(c: sqlite3.Connection, keep_d: float | None = None, batch: int = 5000) -> int:
    cut = datetime.fromtimestamp(time.time() - (KEEP_D if keep_d is None else keep_d) * 86400, UTC).isoformat()
    total = 0
    while True:
        n = c.execute("DELETE FROM pool_trades WHERE rowid IN (SELECT rowid FROM pool_trades WHERE received_at < ?"
                      " LIMIT ?)", (cut, batch)).rowcount
        c.commit()
        total += n
        if n < batch:
            c.execute("DELETE FROM pool_coverage WHERE end_ts < ?",
                      (time.time() - (KEEP_D if keep_d is None else keep_d) * 86400,))
            c.commit()
            return total
        time.sleep(0.05)


def load_state(db: str, st: State, now: float | None = None) -> None:
    """Resume pools still inside their follow window and today's usage (so a restart cannot reset the budget)."""
    now = now or time.time()
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        horizon = TAIL_S if TAIL_ON else FOLLOW_S
        for pool, mint, ts, q, sig in c.execute("SELECT pool, mint, block_ts, quote_mint, signature FROM"
                                                " pool_migrations WHERE block_ts >= ?", (int(now - horizon),)):
            st.pools[pool] = {"mint": mint, "at": float(ts), "quote_mint": q, "seen": now, "head_done": True}
            if sig:
                st.seen_sigs[sig] = now
        for pool, v in st.pools.items():   # tail cursor = newest recorded trade (index pool_trades_pool)
            r = c.execute("SELECT signature, slot, block_ts FROM pool_trades WHERE pool=? ORDER BY slot DESC"
                          " LIMIT 1", (pool,)).fetchone()
            if r:
                v["last_sig"], v["last_slot"], v["last_trade"] = r[0], r[1] or 0, float(r[2]) if r[2] else None
            cov = c.execute("SELECT MAX(end_ts) FROM pool_coverage WHERE pool=?", (pool,)).fetchone()
            if cov and cov[0]:
                v["tail_from"] = float(cov[0])
        for src, msgs, nb, http in c.execute("SELECT source, msgs, bytes, http_calls FROM pool_stream_usage"
                                             " WHERE day=?", (st.budget.day,)):
            st.budget.load(src, msgs or 0, nb or 0, http or 0)
    finally:
        c.close()


async def _main(db: str) -> None:
    stop = asyncio.Event()
    connect(db).close()
    st = State()
    load_state(db, st)
    w = QueuedWriter(db, connect_fn=connect).start()
    sp = status_path(db)

    async def housekeeping() -> None:
        last_prune = 0.0
        while not stop.is_set():
            await asyncio.sleep(STATUS_S)
            try:
                st.prune()
                followed = len(st.desired(MAX_POOLS))
                s = write_status(sp, st, followed)
                w.submit(_save_usage(st))
                print(_now(), "alive", st.mode, f"followed={followed}", f"hot={s['hot_pools']}",
                      f"tail={s['tail']['pools']}/{s['tail']['active']}act http={s['tail']['http_calls']}",
                      json.dumps(s["session"]),
                      f"free_mb={s['free']['mb']} helius_credits={s['helius']['credits']}", w.stats(), flush=True)
                if time.time() - last_prune > 3600:
                    last_prune = time.time()
                    print(_now(), "pruned", await asyncio.wrap_future(w.submit(prune_sync)), flush=True)
            except Exception as exc:  # noqa: BLE001 - housekeeping never takes the stream down
                print(_now(), "housekeeping", type(exc).__name__, str(exc)[:120], flush=True)
    await asyncio.gather(run_stream(w, st, stop), run_backfill(w, st, stop), run_tail(w, st, stop), housekeeping())


def import_archive(db: str, root: str) -> int:
    """History: GOMD Old Faithful 'migrate' lines (READ ONLY - never runs GOMD scripts or writes their files)."""
    c = connect(db)
    n = 0
    for fn in sorted(glob.glob(os.path.join(root, "epoch-*.other.jsonl.gz"))):
        try:
            with gzip.open(fn, "rt") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("kind") != "migrate" or not r.get("pool") or not r.get("mint"):
                        continue
                    n += c.execute(INSERT_MIG, (r["pool"], r["mint"], r.get("signature"), r.get("slot"),
                                                r.get("timestamp"), _now(), "gomd_archive", PUMP_AMM,
                                                r.get("quote_mint"), (r.get("mint_amount") or 0) / 1e6,
                                                (r.get("sol_amount") or 0) / 1e9, r.get("user"), None,
                                                None)).rowcount
        except (EOFError, OSError):   # an epoch still being written ends mid-stream: keep what was read
            pass
        c.commit()
    c.close()
    return n


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=default_db())
    ap.add_argument("--import-archive", metavar="DIR", help="read-only import of GOMD migrate lines")
    a = ap.parse_args()
    if a.import_archive:
        print("imported", import_archive(a.db, a.import_archive))
        return
    asyncio.run(_main(a.db))


if __name__ == "__main__":  # pragma: no cover
    main()
