"""OTHER CHAINS: paper-only scanner of new EVM launches. Its only job is a yes/no on other-chain trading.

PAPER ONLY. This module never signs, swaps, bridges or moves funds. It never imports the signer, never reads
keys, and never reads or prints wallet addresses. It only reads public market APIs and the treasury's
latest balance reading (read-only) to know which chains hold funds.

Pipeline (docs/research/other-chains-go-no-go.md has the pre-registered decision rule):

  discover  GeckoTerminal ``/networks/{net}/new_pools`` (round-robin, 4 req/min, shared GT budget respected)
            + DexScreener latest token profiles / boosts (paid listings; resolved to their newest pair).
            Discovery lag = when we saw the pair - when the pair was created (measured, per chain + source).
  vet       DexScreener pair data -> liquidity floor -> KyberSwap route (the signer's aggregator; router must be
            the pinned router) buy + sell-back quote = real round-trip slippage + DEX fees + gas estimate
            -> GoPlus token security (honeypot, buy/sell tax, cannot-sell). Every skip is counted by reason.
  follow    buyable pairs only: DexScreener snapshot every 30 s for 15 min (+ the 10 min hold if entered).
  paper     one fixed rule (no tuning): momentum signal -> entry at the first FRESH snapshot after
            signal + execution delay (+ bridge time on unfunded chains) -> exit after 10 min or -35 % stop.
            Net = after buy/sell tax, entry/exit slippage + fees, gas (buy+approve+sell) and, for chains with
            no funds, a real Relay bridge quote both ways.

Every decision uses data fetched seconds earlier; nothing is evaluated on a stale snapshot. The loop beats
``feed:other_chains`` in the heartbeat registry.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("noema.other_chains")

HEARTBEAT = "feed:other_chains"
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
KYBER = "https://aggregator-api.kyberswap.com"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security"
RELAY = "https://api.relay.link/quote"
COINBASE = "https://api.coinbase.com/v2/prices/{}-USD/spot"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
ZERO = "0x0000000000000000000000000000000000000000"
# Same pinned router as noema/evm_swap.py (tests assert they match); we only quote, never build or sign.
KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
# Relay needs *some* user for a quote; a burn address keeps NOEMA's wallet out of every request.
QUOTE_USER = "0x000000000000000000000000000000000000dEaD"
KYBER_NO_ROUTE = re.compile(r'"code":\s*40(08|10|11)\b|route not found|token not found', re.IGNORECASE)
UA = {"User-Agent": "noema-paper-scanner/1.0", "x-client-id": "noema-paper"}


@dataclass(frozen=True)
class Chain:
    ds: str            # DexScreener chainId
    gt: str            # GeckoTerminal network id
    chain_id: int
    native: str        # Coinbase symbol of the gas token
    kyber: str         # KyberSwap aggregator slug
    treasury: str | None  # equity_feed_readings source key (None = treasury doesn't read this chain)
    signer: bool       # chain present in evm_swap.SWAP_CHAINS (signer could route a swap here)


CHAINS: dict[str, Chain] = {c.ds: c for c in (
    Chain("base", "base", 8453, "ETH", "base", "evm_base", True),
    Chain("robinhood", "robinhood", 4663, "ETH", "robinhood", "evm_robinhood", True),
    Chain("ethereum", "eth", 1, "ETH", "ethereum", "evm_ethereum", True),
    Chain("bsc", "bsc", 56, "BNB", "bsc", None, False),
    Chain("arbitrum", "arbitrum", 42161, "ETH", "arbitrum", "evm_arbitrum", False),
    Chain("polygon", "polygon_pos", 137, "POL", "polygon", "evm_polygon", True),
)}

# Chains this scanner runs on (Tony 2026-10-22: only chains usable with what we hold). Others are skipped entirely.
DEFAULT_ACTIVE_CHAINS = "base,ethereum,robinhood"


def active_chains(spec: str | None = None) -> dict[str, Chain]:
    """Allowlist from ``spec`` or NOEMA_OTHERCHAIN_CHAINS (comma list of DexScreener chain ids)."""
    raw = spec if spec is not None else os.getenv("NOEMA_OTHERCHAIN_CHAINS", DEFAULT_ACTIVE_CHAINS)
    names = {x.strip().lower() for x in raw.split(",") if x.strip()}
    unknown = sorted(names - set(CHAINS))
    if unknown:
        raise ValueError(f"unknown other-chain(s): {', '.join(unknown)}")
    return {k: v for k, v in CHAINS.items() if k in names}


# ---- pre-registered parameters (frozen; changing any of these is a new study with a new registration) ----
PAPER_SIZE_USD = float(os.getenv("NOEMA_OTHERCHAIN_PAPER_SIZE_USD", "25"))
FUNDED_MIN_USD = 1.0               # a chain counts as funded when the treasury reads >= $1 there
MAX_AGE_AT_DISCOVERY_S = 3600      # older pairs are not "new launches"
MIN_LIQ_USD = max(5000.0, PAPER_SIZE_USD * 200)   # our size moves a fresh x*y pool <= ~1 %
MAX_RT_COST = 0.10                 # Kyber buy+sell-back loss above 10 % = not tradable at our size
MAX_TAX = 0.10                     # GoPlus buy or sell tax above 10 % = skip
FOLLOW_S = 900                     # follow window (like the Solana desk)
HOLD_S = 600                       # paper hold after entry
STOP = -0.35                       # observed-price stop
EXEC_DELAY_S = 20                  # decide -> swap confirmed; entry = first snapshot after signal + this
SNAP_EVERY_S = 30
SNAP_MAX_AGE_S = 60                # a snapshot older than this is never used for a decision
SIGNAL = {"buys_m5_min": 10, "buy_sell_ratio_min": 1.5, "vol_m5_min_usd": 1000.0, "liq_keep_min": 0.7}
DEAD_LIQ_FRAC = 0.5                # liquidity below half its first reading = dead
GAS_LEGS = 2.5                     # buy + sell + ERC-20 approve (~half a swap)
UNPRICED_EXIT_RET = -1.0           # a position whose pair vanishes is booked as a total loss
SNAP_RETENTION_DAYS = 14

PREREG: dict[str, Any] = {
    "study": "other_chains_v1",
    "decision_unit": "chain (each chain decided separately)",
    "rule": {"signal": SIGNAL, "exec_delay_s": EXEC_DELAY_S, "hold_s": HOLD_S, "stop": STOP,
             "follow_s": FOLLOW_S, "min_liq_usd": MIN_LIQ_USD, "max_rt_cost": MAX_RT_COST, "max_tax": MAX_TAX,
             "paper_size_usd": PAPER_SIZE_USD, "max_age_at_discovery_s": MAX_AGE_AT_DISCOVERY_S},
    "costs": "buy/sell tax (GoPlus) + Kyber round-trip slippage & DEX fees (exit leg scaled by liquidity) + "
             "gas x2.5 (Kyber gasUsd) + Relay bridge in AND out for chains without funds; unpriced exit = -100%",
    "min_trades_per_chain": 100,
    "min_heldout_trades": 30,
    "min_days": 7,
    "split": "chronological by UTC day: first 60% of days = discovery, last 40% (rounded up) = held-out",
    "heldout_pass": "mean net return/trade > 0 AND bootstrap lower bound > 0 at 1-0.05/k (k = chains tested, Bonferroni)",
    "each_day": "among UTC days with >= 5 trades: at most 1 losing day overall and zero losing held-out days",
    "early_no_go": "n >= 100 and bootstrap upper bound (95%) of mean net < 0",
    "decision_point": "per chain, the first UTC day end at which min trades, min days and min held-out are all met; "
                      "the verdict then is final for v1 (no waiting for a better one)",
    "no_tuning": "single fixed rule; no parameter changes after registration",
    "go_means": "chain becomes eligible for an owner-reviewed proposal; still paper until Tony decides",
}


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def _epoch(v: Any) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    try:
        s = str(v)
        return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s).timestamp()
    except ValueError:
        return None


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ---- storage --------------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS oc_preregistration(id INTEGER PRIMARY KEY, created_at TEXT, sha256 TEXT UNIQUE, criteria_json TEXT);
CREATE TABLE IF NOT EXISTS oc_pairs(
  chain TEXT, pair TEXT, token TEXT, symbol TEXT, dex TEXT, quote_symbol TEXT, source TEXT,
  created_at REAL, seen_at REAL, lag_s REAL, stage TEXT, skip_reason TEXT, tries INTEGER DEFAULT 0,
  funded INTEGER, signer_chain INTEGER, liq_seen REAL, buy_tax REAL, sell_tax REAL, rt_cost REAL, gas_usd REAL,
  bridge_s REAL, bridge_cost_usd REAL, first_liq REAL, first_price REAL, follow_until REAL,
  signal_at REAL, entry_at REAL, entry_price REAL, entry_liq REAL, exit_at REAL, exit_price REAL, exit_liq REAL,
  exit_reason TEXT, gross_ret REAL, net_ret REAL, cost_usd REAL, day TEXT, updated_at REAL,
  PRIMARY KEY(chain, pair));
CREATE INDEX IF NOT EXISTS oc_pairs_stage ON oc_pairs(stage);
CREATE INDEX IF NOT EXISTS oc_pairs_token ON oc_pairs(chain, token);
CREATE TABLE IF NOT EXISTS oc_snaps(chain TEXT, pair TEXT, at REAL, price REAL, liq REAL, vol_m5 REAL,
  buys_m5 INTEGER, sells_m5 INTEGER, fdv REAL);
CREATE INDEX IF NOT EXISTS oc_snaps_pair ON oc_snaps(chain, pair, at);
CREATE TABLE IF NOT EXISTS oc_bridge(chain TEXT, at REAL, origin TEXT, time_s REAL, cost_usd REAL, amount_usd REAL,
  ok INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS oc_polls(at REAL, chain TEXT, source TEXT, ok INTEGER, n INTEGER, n_new INTEGER, error TEXT);
"""


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=10000")
    c.executescript(SCHEMA)
    return c


def preregister(c: sqlite3.Connection, now: float | None = None) -> str:
    """Store the decision rule (hash + timestamp) once, before any outcome exists. Idempotent."""
    blob = json.dumps(PREREG, sort_keys=True)
    sha = hashlib.sha256(blob.encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO oc_preregistration(created_at, sha256, criteria_json) VALUES(?,?,?)",
              (_iso(now or _now()), sha, blob))
    c.commit()
    return sha


# ---- cost model (pure) ----------------------------------------------------------------------------------
def leg_costs(rt_cost: float, entry_liq: float, exit_liq: float | None) -> tuple[float, float]:
    """Split the measured Kyber round trip into an entry and an exit leg; the exit leg grows as liquidity shrinks."""
    half = max(0.0, rt_cost) / 2
    if not exit_liq or exit_liq <= 0:
        return half, 0.5
    return half, min(0.5, half * max(1.0, entry_liq / exit_liq))


def paper_net(gross_ret: float, *, size: float, buy_tax: float, sell_tax: float, rt_cost: float,
              entry_liq: float, exit_liq: float | None, gas_usd: float, bridge_cost_usd: float = 0.0
              ) -> tuple[float, float]:
    """(net return, total cost in USD) for one paper round trip. bridge_cost_usd is the one-way quote; we pay it twice."""
    e_leg, x_leg = leg_costs(rt_cost, entry_liq, exit_liq)
    held = size * (1 - buy_tax) * (1 - e_leg)
    proceeds = max(0.0, held * (1 + gross_ret)) * (1 - sell_tax) * (1 - x_leg)
    fixed = gas_usd * GAS_LEGS + 2 * bridge_cost_usd
    net = proceeds - size - fixed
    naive = size * gross_ret
    return net / size, naive - net


def signal_ok(s: dict[str, Any], first_liq: float) -> bool:
    b, sl = s.get("buys_m5") or 0, s.get("sells_m5") or 0
    return (b >= SIGNAL["buys_m5_min"] and b >= SIGNAL["buy_sell_ratio_min"] * max(sl, 1)
            and (s.get("vol_m5") or 0) >= SIGNAL["vol_m5_min_usd"] and (s.get("liq") or 0) >= MIN_LIQ_USD
            and (s.get("liq") or 0) >= SIGNAL["liq_keep_min"] * first_liq)


def security_verdict(sec: dict[str, Any] | None) -> tuple[str | None, float, float]:
    """(skip reason or None, buy_tax, sell_tax) from a GoPlus record."""
    if not sec:
        return "security_unknown", 0.0, 0.0
    bt, st = _f(sec.get("buy_tax")), _f(sec.get("sell_tax"))
    if str(sec.get("is_honeypot")) == "1":
        return "honeypot", bt or 0.0, st or 0.0
    if str(sec.get("cannot_sell_all")) == "1" or str(sec.get("cannot_buy")) == "1":
        return "cannot_trade", bt or 0.0, st or 0.0
    if bt is None or st is None:
        # GoPlus leaves taxes blank until it can simulate; unknown tax is not buyable
        return "tax_unknown", bt or 0.0, st or 0.0
    if bt > MAX_TAX or st > MAX_TAX:
        return "high_tax", bt, st
    return None, bt, st


# ---- network ----------------------------------------------------------------------------------------------
class Limiter:
    def __init__(self, per_min: float) -> None:
        self.gap = 60.0 / per_min
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            d = self.next - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)
            self.next = max(self.next, time.monotonic()) + self.gap

    def backoff(self, s: float) -> None:
        self.next = max(self.next, time.monotonic() + s)


class Scanner:
    def __init__(self, db: str, main_db: str, client: Any = None, chains: dict[str, Chain] | None = None) -> None:
        import httpx
        self.chains = chains if chains is not None else active_chains()
        self.c = connect(db)
        self.main_db = main_db
        self.http = client or httpx.AsyncClient(timeout=15, headers=UA)
        # well under each provider's published free limit
        self.lim = {"gt": Limiter(4), "ds_prof": Limiter(20), "ds_pairs": Limiter(120),
                    "kyber": Limiter(30), "goplus": Limiter(10), "relay": Limiter(6), "cb": Limiter(30)}
        self.native_px: dict[str, tuple[float, float]] = {}
        self.funded: dict[str, float] = {}
        self.funded_at = 0.0
        self.bridge: dict[str, tuple[float, float, float]] = {}  # chain -> (time_s, cost_usd, at)
        self.vet_q: deque[tuple[str, str]] = deque()
        self.errors: dict[str, str] = {}
        self.last_ok: dict[str, float] = {}
        self.passes = 0
        self.sha = self.register()

    def register(self) -> str:
        """Pre-register this arm's rule in its DB (v1 = PREREG; a later arm overrides with its own)."""
        return preregister(self.c)

    async def get(self, lim: str, url: str, **kw: Any) -> Any:
        await self.lim[lim].wait()
        r = await self.http.get(url, **kw)
        if r.status_code == 429:
            self.lim[lim].backoff(30 + random.random() * 10)
            raise RuntimeError(f"{lim} HTTP 429")
        r.raise_for_status()
        return r.json()

    def poll(self, chain: str, source: str, ok: bool, n: int = 0, n_new: int = 0, err: Any = None) -> None:
        self.c.execute("INSERT INTO oc_polls VALUES(?,?,?,?,?,?,?)", (_now(), chain, source, int(ok), n, n_new,
                                                                        None if err is None else str(err)[:160]))
        self.c.commit()
        if ok:
            self.last_ok[source] = _now()
            self.errors.pop(source, None)
        else:
            self.errors[source] = str(err)[:120]

    # -- funding / prices / bridge --
    def refresh_funded(self) -> None:
        """Treasury balance per EVM chain from the newest equity reading (read-only, amounts only)."""
        if _now() - self.funded_at < 300:
            return
        try:
            m = sqlite3.connect(f"file:{self.main_db}?mode=ro", uri=True, timeout=5)
            row = m.execute("SELECT at, sources_json FROM equity_feed_readings ORDER BY rowid DESC LIMIT 1").fetchone()
            m.close()
        except sqlite3.Error as exc:
            self.errors["treasury"] = str(exc)[:120]
            return
        if not row:
            return
        src = json.loads(row[1] or "{}")
        fresh = (_now() - (_epoch(row[0]) or 0)) < 900
        out = {}
        for ch in CHAINS.values():
            v = src.get(ch.treasury or "") or {}
            out[ch.ds] = (_f(v.get("equity_usd")) or 0.0) if fresh and v.get("status") == "ok" else 0.0
        self.funded, self.funded_at = out, _now()

    def is_funded(self, chain: str) -> bool:
        return self.funded.get(chain, 0.0) >= FUNDED_MIN_USD

    async def native_price(self, sym: str) -> float | None:
        p = self.native_px.get(sym)
        if p and _now() - p[1] < 60:
            return p[0]
        try:
            d = await self.get("cb", COINBASE.format(sym))
            v = _f(d["data"]["amount"])
            if v and v > 0:
                self.native_px[sym] = (v, _now())
                return v
        except Exception as exc:  # noqa: BLE001
            self.errors["coinbase"] = str(exc)[:120]
        return p[0] if p and _now() - p[1] < 120 else None   # never older than 2 min

    async def bridge_quote(self, chain: str) -> tuple[float, float] | None:
        """Real Relay quote from the funded chain with the most money to ``chain`` at paper size (hourly)."""
        b = self.bridge.get(chain)
        if b and _now() - b[2] < 3600:
            return b[0], b[1]
        origins = sorted((v, k) for k, v in self.funded.items() if v >= FUNDED_MIN_USD and k != chain)
        if not origins:
            return None
        origin = CHAINS[origins[-1][1]]
        px = await self.native_price(origin.native)
        if not px:
            return None
        amt = str(int(PAPER_SIZE_USD / px * 1e18))
        body = {"user": QUOTE_USER, "originChainId": origin.chain_id, "destinationChainId": CHAINS[chain].chain_id,
                "originCurrency": ZERO, "destinationCurrency": ZERO, "amount": amt, "tradeType": "EXACT_INPUT"}
        try:
            await self.lim["relay"].wait()
            r = await self.http.post(RELAY, json=body)
            r.raise_for_status()
            d = r.json()["details"]
            t = float(d.get("timeEstimate") or 0) + 5   # + origin confirmation
            cost = max(0.0, float(d["currencyIn"]["amountUsd"]) - float(d["currencyOut"]["amountUsd"]))
            gas = _f(((r.json().get("fees") or {}).get("gas") or {}).get("amountUsd")) or 0.0
            cost += gas
            self.bridge[chain] = (t, cost, _now())
            self.c.execute("INSERT INTO oc_bridge VALUES(?,?,?,?,?,?,?,?)",
                           (chain, _now(), origin.ds, t, cost, PAPER_SIZE_USD, 1, None))
            self.c.commit()
            return t, cost
        except Exception as exc:  # noqa: BLE001
            self.c.execute("INSERT INTO oc_bridge VALUES(?,?,?,?,?,?,?,?)",
                           (chain, _now(), origin.ds, None, None, PAPER_SIZE_USD, 0, str(exc)[:160]))
            self.c.commit()
            return (b[0], b[1]) if b and _now() - b[2] < 3 * 3600 else None

    # -- discovery --
    def add_pair(self, chain: str, pair: str, created: float | None, source: str, token: str | None = None,
                 symbol: str | None = None, dex: str | None = None, quote: str | None = None) -> bool:
        if not pair or created is None or chain not in self.chains:
            return False
        pair = pair.lower()
        now = _now()
        lag = now - created
        cur = self.c.execute("INSERT OR IGNORE INTO oc_pairs(chain,pair,token,symbol,dex,quote_symbol,source,created_at,"
                             "seen_at,lag_s,stage,signer_chain,funded,day,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (chain, pair, (token or "").lower() or None, symbol, dex, quote, source, created, now, lag,
                              "seen", int(CHAINS[chain].signer), int(self.is_funded(chain)),
                              datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d"), now))
        if not cur.rowcount:
            return False
        if lag > MAX_AGE_AT_DISCOVERY_S:
            self.skip(chain, pair, "too_old")
        else:
            self.vet_q.append((chain, pair))
        return True

    def skip(self, chain: str, pair: str, reason: str, **extra: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in extra)
        self.c.execute(f"UPDATE oc_pairs SET stage='skipped', skip_reason=?, updated_at=?{', ' + sets if sets else ''} "
                       "WHERE chain=? AND pair=?", (reason, _now(), *extra.values(), chain, pair))

    async def gt_loop(self) -> None:
        order = list(self.chains)
        i = 0
        while True:
            ch = self.chains[order[i % len(order)]]
            i += 1
            try:
                d = await self.get("gt", f"{GT}/networks/{ch.gt}/new_pools", params={"page": 1})
                n_new = 0
                rows = d.get("data") or []
                for p in rows:
                    a = p.get("attributes") or {}
                    base = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                    dex = (((p.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id")
                    name = str(a.get("name") or "")
                    n_new += self.add_pair(ch.ds, a.get("address"), _epoch(a.get("pool_created_at")), "geckoterminal",
                                           base.split("_", 1)[-1] if base else None, name.split(" / ")[0][:24], dex,
                                           (name.split(" / ") + [""])[1][:16] or None)
                self.c.commit()
                self.poll(ch.ds, "geckoterminal", True, len(rows), n_new)
            except Exception as exc:  # noqa: BLE001
                self.poll(ch.ds, "geckoterminal", False, err=exc)

    async def ds_loop(self) -> None:
        while True:
            for path, src in (("token-profiles/latest/v1", "ds_profile"), ("token-boosts/latest/v1", "ds_boost")):
                try:
                    rows = await self.get("ds_prof", f"{DS}/{path}")
                    toks = {(r.get("chainId"), str(r.get("tokenAddress") or "").lower()) for r in rows or []
                            if r.get("chainId") in self.chains and r.get("tokenAddress")}
                    n_new = 0
                    for chain, tok in toks:
                        if self.c.execute("SELECT 1 FROM oc_pairs WHERE chain=? AND token=? LIMIT 1", (chain, tok)).fetchone():
                            continue
                        try:
                            pairs = await self.get("ds_pairs", f"{DS}/token-pairs/v1/{chain}/{tok}")
                        except Exception as e:  # noqa: BLE001
                            log.debug("token-pairs %s failed: %s", chain, e)
                            continue
                        pairs = [p for p in pairs or [] if p.get("pairCreatedAt")]
                        if not pairs:
                            continue
                        p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
                        n_new += self.add_pair(chain, p.get("pairAddress"), _epoch(p.get("pairCreatedAt")), src, tok,
                                               (p.get("baseToken") or {}).get("symbol"), p.get("dexId"),
                                               (p.get("quoteToken") or {}).get("symbol"))
                    self.c.commit()
                    self.poll("*", src, True, len(rows or []), n_new)
                except Exception as exc:  # noqa: BLE001
                    self.poll("*", src, False, err=exc)
            await asyncio.sleep(45)

    async def ds_pairs(self, chain: str, pairs: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(pairs), 30):
            d = await self.get("ds_pairs", f"{DS}/latest/dex/pairs/{chain}/{','.join(pairs[i:i + 30])}")
            for p in (d or {}).get("pairs") or []:
                out[str(p.get("pairAddress") or "").lower()] = p
        return out

    # -- vetting --
    async def kyber(self, chain: Chain, t_in: str, t_out: str, amount: int) -> dict[str, Any] | None:
        await self.lim["kyber"].wait()
        r = await self.http.get(f"{KYBER}/{chain.kyber}/api/v1/routes",
                                params={"tokenIn": t_in, "tokenOut": t_out, "amountIn": str(amount)})
        if r.status_code == 429:
            self.lim["kyber"].backoff(30 + random.random() * 10)
            raise RuntimeError("kyber HTTP 429")
        if r.status_code == 400 and KYBER_NO_ROUTE.search(r.text or ""):
            return None   # Kyber answers 400 "route not found" / unknown token: not buyable, not an error
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0 or not d.get("data"):
            return None
        return d["data"]

    async def goplus(self, chain: Chain, token: str) -> dict[str, Any] | None:
        d = await self.get("goplus", f"{GOPLUS}/{chain.chain_id}", params={"contract_addresses": token})
        if d.get("code") != 1:
            raise RuntimeError(f"goplus code {d.get('code')}")
        return (d.get("result") or {}).get(token.lower())

    async def vet(self, chain: str, pair: str) -> None:
        ch = CHAINS[chain]
        row = self.c.execute("SELECT * FROM oc_pairs WHERE chain=? AND pair=?", (chain, pair)).fetchone()
        if not row or row["stage"] != "seen":
            return
        if _now() - row["seen_at"] > 600:
            self.skip(chain, pair, "queue_timeout")
            return
        p = (await self.ds_pairs(chain, [pair])).get(pair)
        if not p:
            tries = (row["tries"] or 0) + 1
            if tries >= 4:
                self.skip(chain, pair, "not_indexed")
            else:
                self.c.execute("UPDATE oc_pairs SET tries=? WHERE chain=? AND pair=?", (tries, chain, pair))
                asyncio.get_running_loop().call_later(30, self.vet_q.append, (chain, pair))
            return
        liq = _f((p.get("liquidity") or {}).get("usd")) or 0.0
        token = str((p.get("baseToken") or {}).get("address") or row["token"] or "").lower()
        self.c.execute("UPDATE oc_pairs SET liq_seen=?, token=?, symbol=coalesce(symbol,?), dex=?, quote_symbol=? "
                       "WHERE chain=? AND pair=?", (liq, token, (p.get("baseToken") or {}).get("symbol"),
                                                    p.get("dexId"), (p.get("quoteToken") or {}).get("symbol"), chain, pair))
        if liq < MIN_LIQ_USD:
            self.skip(chain, pair, "low_liquidity")
            return
        px = await self.native_price(ch.native)
        if not px:
            self.skip(chain, pair, "stale_native_price")
            return
        wei = int(PAPER_SIZE_USD / px * 1e18)
        buy = await self.kyber(ch, NATIVE, token, wei)
        if not buy:
            self.skip(chain, pair, "unroutable")
            return
        if str(buy.get("routerAddress", "")).lower() != KYBER_ROUTER.lower():
            self.skip(chain, pair, "router_not_pinned")
            return
        summ = buy["routeSummary"]
        sell = await self.kyber(ch, token, NATIVE, int(summ["amountOut"]))
        if not sell:
            self.skip(chain, pair, "unroutable_sell")
            return
        rt = 1 - int(sell["routeSummary"]["amountOut"]) / max(wei, 1)
        gas = _f(summ.get("gasUsd")) or 0.0
        self.c.execute("UPDATE oc_pairs SET rt_cost=?, gas_usd=? WHERE chain=? AND pair=?", (rt, gas, chain, pair))
        if rt > MAX_RT_COST:
            self.skip(chain, pair, "slippage_too_high")
            return
        sec = await self.goplus(ch, token)
        reason, bt, st = security_verdict(sec)
        if reason in ("security_unknown", "tax_unknown") and (row["tries"] or 0) < 2:
            # brand-new tokens are often not simulated yet: one retry 90 s later
            self.c.execute("UPDATE oc_pairs SET tries=? WHERE chain=? AND pair=?", ((row["tries"] or 0) + 2, chain, pair))
            asyncio.get_running_loop().call_later(90, self.vet_q.append, (chain, pair))
            return
        if reason:
            self.skip(chain, pair, reason, buy_tax=bt, sell_tax=st)
            return
        funded = self.is_funded(chain)
        bridge_s = bridge_cost = 0.0
        if not funded:
            bq = await self.bridge_quote(chain)
            if not bq:
                self.skip(chain, pair, "no_bridge_quote", buy_tax=bt, sell_tax=st)
                return
            bridge_s, bridge_cost = bq
        now = _now()
        self.c.execute("UPDATE oc_pairs SET stage='followed', buy_tax=?, sell_tax=?, funded=?, bridge_s=?, "
                       "bridge_cost_usd=?, follow_until=?, updated_at=? WHERE chain=? AND pair=?",
                       (bt, st, int(funded), bridge_s, bridge_cost, now + FOLLOW_S, now, chain, pair))

    async def vet_loop(self) -> None:
        while True:
            if not self.vet_q:
                await asyncio.sleep(1)
                continue
            chain, pair = self.vet_q.popleft()
            try:
                await self.vet(chain, pair)
                self.poll(chain, "vet", True)
            except Exception as exc:  # noqa: BLE001
                self.poll(chain, "vet", False, err=exc)
                r = self.c.execute("SELECT tries FROM oc_pairs WHERE chain=? AND pair=?", (chain, pair)).fetchone()
                if r and (r[0] or 0) < 3:
                    self.c.execute("UPDATE oc_pairs SET tries=coalesce(tries,0)+1 WHERE chain=? AND pair=?", (chain, pair))
                    asyncio.get_running_loop().call_later(20, self.vet_q.append, (chain, pair))
                else:
                    self.skip(chain, pair, "vet_error")
            self.c.commit()

    # -- follow + paper --
    def step(self, r: sqlite3.Row, s: dict[str, Any] | None, now: float) -> None:
        """Advance one followed pair with a fresh snapshot ``s`` (None = pair not returned)."""
        chain, pair = r["chain"], r["pair"]
        upd: dict[str, Any] = {}
        if s is not None and r["first_liq"] is None:
            upd.update(first_liq=s["liq"], first_price=s["price"])
        first_liq = r["first_liq"] or (s or {}).get("liq") or 0
        if r["entry_at"] is None:
            ready_at = (r["signal_at"] or 0) + EXEC_DELAY_S + (r["bridge_s"] or 0)
            if r["signal_at"] is not None and s is not None and now >= ready_at:
                if now - ready_at > 2 * SNAP_EVERY_S + 15:
                    upd.update(signal_at=None)   # data gap: the signal is stale, never enter on it
                else:
                    upd.update(entry_at=now, entry_price=s["price"], entry_liq=s["liq"], follow_until=now + HOLD_S)
            elif r["signal_at"] is None and s is not None and (s["liq"] < DEAD_LIQ_FRAC * first_liq):
                upd.update(stage="closed", exit_reason="dead_before_signal")
            elif r["signal_at"] is None and s is not None and signal_ok(s, first_liq):
                upd.update(signal_at=now)
            elif now > r["follow_until"]:
                upd.update(stage="closed", exit_reason="no_signal" if r["signal_at"] is None else "signal_no_entry")
        else:
            ret = None if s is None or not s["price"] else s["price"] / r["entry_price"] - 1
            reason = None
            if ret is not None and ret <= STOP:
                reason = "stop"
            elif now >= r["follow_until"] and ret is not None:
                reason = "time"
            elif now >= r["follow_until"] + 120:
                reason = "unpriced"   # no priced snapshot for 2 min past the hold: booked as a total loss
            if reason:
                gross = ret if ret is not None else UNPRICED_EXIT_RET
                net, cost = paper_net(gross, size=PAPER_SIZE_USD, buy_tax=r["buy_tax"] or 0, sell_tax=r["sell_tax"] or 0,
                                      rt_cost=r["rt_cost"] or 0, entry_liq=r["entry_liq"] or 0,
                                      exit_liq=(s or {}).get("liq"), gas_usd=r["gas_usd"] or 0,
                                      bridge_cost_usd=0.0 if r["funded"] else (r["bridge_cost_usd"] or 0))
                upd.update(stage="closed", exit_at=now, exit_price=(s or {}).get("price"), exit_liq=(s or {}).get("liq"),
                           exit_reason=reason, gross_ret=gross, net_ret=net, cost_usd=cost,
                           day=datetime.fromtimestamp(r["entry_at"], UTC).strftime("%Y-%m-%d"))
        if upd:
            upd["updated_at"] = now
            self.c.execute(f"UPDATE oc_pairs SET {', '.join(k + '=?' for k in upd)} WHERE chain=? AND pair=?",
                           (*upd.values(), chain, pair))

    async def follow_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            rows = self.c.execute("SELECT * FROM oc_pairs WHERE stage='followed'").fetchall()
            by: dict[str, list[sqlite3.Row]] = {}
            for r in rows:
                by.setdefault(r["chain"], []).append(r)
            for chain, rs in by.items():
                try:
                    got = await self.ds_pairs(chain, [r["pair"] for r in rs])
                    now = _now()   # snapshot time = right after the fetch (never older than this loop pass)
                    for r in rs:
                        p = got.get(r["pair"])
                        s = None
                        if p and _f(p.get("priceUsd")):
                            m5 = ((p.get("txns") or {}).get("m5") or {})
                            s = {"price": _f(p.get("priceUsd")), "liq": _f((p.get("liquidity") or {}).get("usd")) or 0.0,
                                 "vol_m5": _f((p.get("volume") or {}).get("m5")) or 0.0, "buys_m5": int(m5.get("buys") or 0),
                                 "sells_m5": int(m5.get("sells") or 0), "fdv": _f(p.get("fdv"))}
                            self.c.execute("INSERT INTO oc_snaps VALUES(?,?,?,?,?,?,?,?,?)",
                                           (chain, r["pair"], now, s["price"], s["liq"], s["vol_m5"], s["buys_m5"],
                                            s["sells_m5"], s["fdv"]))
                        self.step(r, s, now)
                    self.c.commit()
                    self.poll(chain, "follow", True, len(rs))
                except Exception as exc:  # noqa: BLE001
                    # no fresh data -> no decisions this pass (stale data is never acted on)
                    self.poll(chain, "follow", False, err=exc)
            await asyncio.sleep(max(1.0, SNAP_EVERY_S - (time.monotonic() - t0)))

    async def housekeeping(self) -> None:
        from .heartbeats import beat
        while True:
            self.refresh_funded()
            now = _now()
            fresh = [k for k in ("geckoterminal", "follow") if now - self.last_ok.get(k, 0) < 180]
            ok = "geckoterminal" in fresh
            beat(HEARTBEAT, ok, None if ok else (self.errors.get("geckoterminal") or "no fresh discovery"), cadence_s=60)
            self.passes += 1
            if self.passes % 2 == 1:   # ~once a minute: keeps the loop heartbeat's log activity honest
                f = dict(self.c.execute("SELECT stage, count(*) FROM oc_pairs GROUP BY 1").fetchall())
                log.info("funnel %s · queue %d · discovery %s", json.dumps(f, sort_keys=True), len(self.vet_q),
                         "ok" if ok else "STALE")
            self.c.execute("DELETE FROM oc_snaps WHERE at < ?", (now - SNAP_RETENTION_DAYS * 86400,))
            self.c.execute("DELETE FROM oc_polls WHERE at < ?", (now - 3 * 86400,))
            self.c.commit()
            await asyncio.sleep(30)

    def retire_dropped(self) -> int:
        """Close open rows on chains no longer in the allowlist (no result booked; they never count as trades)."""
        dropped = [c for c in CHAINS if c not in self.chains]
        if not dropped:
            return 0
        q = ",".join("?" * len(dropped))
        n = self.c.execute(f"UPDATE oc_pairs SET stage='closed', exit_reason='chain_dropped', updated_at=? "
                           f"WHERE stage IN ('seen','followed') AND chain IN ({q})", (_now(), *dropped)).rowcount
        self.c.commit()
        return n

    async def run(self) -> None:
        self.refresh_funded()
        self.retire_dropped()
        # resume: anything 'seen' at restart goes back into the queue (old ones time out there)
        for r in self.c.execute("SELECT chain, pair FROM oc_pairs WHERE stage='seen'"):
            self.vet_q.append((r[0], r[1]))
        await asyncio.gather(self.gt_loop(), self.ds_loop(), self.vet_loop(), self.follow_loop(), self.housekeeping())


# ---- evaluation (pre-registered) -------------------------------------------------------------------------
def _boot_bounds(xs: list[float], alpha: float, n_boot: int = 4000, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(n_boot))
    lo = means[max(0, int(alpha * n_boot) - 1)]
    hi = means[min(n_boot - 1, int((1 - alpha) * n_boot))]
    return lo, hi


def verdict_for(trades: list[tuple[str, float]], k_chains: int) -> dict[str, Any]:
    """trades = [(utc_day, net_ret)] for one chain. Applies the pre-registered go/no-go rule."""
    n = len(trades)
    days = sorted({d for d, _ in trades})
    out: dict[str, Any] = {"n": n, "days": len(days), "verdict": "collecting"}
    if n:
        xs = [x for _, x in trades]
        out.update(mean=statistics.fmean(xs), median=statistics.median(xs), win=sum(x > 0 for x in xs) / n)
    if n >= PREREG["min_trades_per_chain"]:
        _, hi = _boot_bounds([x for _, x in trades], 0.05)
        out["all_hi95"] = hi
        if hi < 0:
            out["verdict"] = "no_go"
            out["why"] = "early no-go: upper bound of mean net < 0"
            return out
    if n < PREREG["min_trades_per_chain"] or len(days) < PREREG["min_days"]:
        out["why"] = (f"need {PREREG['min_trades_per_chain']} trades over {PREREG['min_days']} days "
                      f"(have {n} over {len(days)})")
        return out
    n_hold = math.ceil(len(days) * 0.4)
    hold_days = set(days[-n_hold:])
    held = [x for d, x in trades if d in hold_days]
    out["heldout_n"] = len(held)
    if len(held) < PREREG["min_heldout_trades"]:
        out["why"] = f"held-out has {len(held)} trades; need {PREREG['min_heldout_trades']}"
        return out
    lo, _ = _boot_bounds(held, 0.05 / max(1, k_chains))
    out.update(heldout_mean=statistics.fmean(held), heldout_lo=lo)
    per_day = {}
    for d, x in trades:
        per_day.setdefault(d, []).append(x)
    day_means = {d: statistics.fmean(v) for d, v in per_day.items() if len(v) >= 5}
    losing = [d for d, m in day_means.items() if m <= 0]
    held_losing = [d for d in losing if d in hold_days]
    out.update(losing_days=len(losing), heldout_losing_days=len(held_losing))
    ok = out["heldout_mean"] > 0 and lo > 0 and len(losing) <= 1 and not held_losing
    out["verdict"] = "go" if ok else "no_go"
    out["why"] = "passes held-out, bound and each-day tests" if ok else "fails held-out mean/bound or a losing day"
    return out


def board(db: str, chains: dict[str, Chain] | None = None) -> dict[str, Any]:
    """Read-only dashboard payload: per-chain funnel, discovery lag, paper results and pre-registered verdict."""
    active = chains if chains is not None else active_chains()
    if not os.path.isfile(db):
        return {"status": "not started", "paper_only": True}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        pre = c.execute("SELECT created_at, sha256 FROM oc_preregistration ORDER BY id LIMIT 1").fetchone()
        chains = []
        for name, ch in active.items():
            f = c.execute("SELECT count(*) seen, sum(stage='skipped') skipped, sum(stage IN ('followed','closed')) followed,"
                          " sum(signal_at IS NOT NULL) signals, sum(entry_at IS NOT NULL) entries,"
                          " sum(exit_reason='dead_before_signal') dead, max(funded) funded, min(seen_at) first"
                          " FROM oc_pairs WHERE chain=?", (name,)).fetchone()
            skips = {r[0]: r[1] for r in c.execute("SELECT skip_reason, count(*) FROM oc_pairs WHERE chain=? AND "
                                                   "stage='skipped' GROUP BY 1 ORDER BY 2 DESC", (name,))}
            # steady-state lag: only pairs created after we started watching this chain (excludes the start-up backlog)
            lags = sorted(r[0] for r in c.execute(
                "SELECT lag_s FROM oc_pairs WHERE chain=? AND source='geckoterminal' AND lag_s IS NOT NULL AND "
                "created_at >= (SELECT min(seen_at) FROM oc_pairs WHERE chain=?)", (name, name)))
            trades = [(r[0], r[1]) for r in c.execute("SELECT day, net_ret FROM oc_pairs WHERE chain=? AND "
                                                       "net_ret IS NOT NULL ORDER BY entry_at", (name,))]
            gross = [r[0] for r in c.execute("SELECT gross_ret FROM oc_pairs WHERE chain=? AND gross_ret IS NOT NULL", (name,))]
            br = c.execute("SELECT time_s, cost_usd, origin FROM oc_bridge WHERE chain=? AND ok=1 ORDER BY at DESC LIMIT 1",
                           (name,)).fetchone()
            v = verdict_for(trades, len(active))   # k = chains tested (pre-registered wording)
            chains.append({
                "chain": name, "funded": bool(f["funded"]), "signer_chain": ch.signer,
                "seen": f["seen"] or 0, "skipped": f["skipped"] or 0, "buyable_followed": f["followed"] or 0,
                "signals": f["signals"] or 0, "entries": f["entries"] or 0, "dead": f["dead"] or 0, "skips": skips,
                "lag_median_s": round(statistics.median(lags)) if lags else None,
                "lag_p10_s": round(lags[len(lags) // 10]) if lags else None, "lag_n": len(lags),
                "closed": len(trades), "gross_mean": statistics.fmean(gross) if gross else None,
                "bridge": dict(br) if br else None, "verdict": v, "first_seen": _iso(f["first"]),
            })
        last = c.execute("SELECT max(at) FROM oc_polls WHERE ok=1").fetchone()[0]
    finally:
        c.close()
    return {"paper_only": True, "study": PREREG["study"], "preregistered_at": pre["created_at"] if pre else None,
            "prereg_sha256": pre["sha256"] if pre else None, "paper_size_usd": PAPER_SIZE_USD,
            "last_poll_at": _iso(last), "chains": chains}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Paper-only other-chain launch scanner (never trades).")
    ap.add_argument("--db", default=os.getenv("NOEMA_OTHERCHAIN_DB", "data/other_chains.db"))
    ap.add_argument("--main-db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--chains", default=os.getenv("NOEMA_OTHERCHAIN_CHAINS", DEFAULT_ACTIVE_CHAINS),
                    help="comma allowlist of chains to scan (others are skipped entirely)")
    ap.add_argument("--board", action="store_true", help="print the board JSON and exit")
    a = ap.parse_args(argv)
    chains = active_chains(a.chains)
    if a.board:
        print(json.dumps(board(a.db, chains), indent=1, default=str))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("other_chains paper scanner starting: chains=%s size=$%s", ",".join(chains), PAPER_SIZE_USD)
    asyncio.run(Scanner(a.db, a.main_db, chains=chains).run())


if __name__ == "__main__":
    main()
