"""Smart-wallet study + SMART-FOLLOW paper agent (PAPER / READ ONLY - nothing here signs or trades).

Data: pump.fun trades already captured by noema.launch_stream (launch_trades, early-life of every new coin, plus
every trade of *watched* wallets and 30 min of trades on coins they buy) + optional Helius backfill of a candidate
wallet's recent history (getSignaturesForAddress/getTransaction, throttled, key never logged).

Vetting (strictly prospective): a wallet is "vetted at time T" only from positions it CLOSED before T:
  n >= MIN_N closed positions, win rate >= 50%, realized PnL > 0 even after removing its single best trade,
  bootstrap 95% lower bound of mean PnL/position > 0, and it is not a dev (creator) / sniper (entries < 2 s after
  create) / bundler (shares a tx with other buyers) / bag-holder (>50% of older entries never closed).
SMART-FOLLOW: when a vetted wallet buys a coin <= 15 min old, paper-enter at the first trade >= 2 s later, exit on
TP +40% / SL -25% / the wallet selling / 20 min, net of 2%/side (fees + slippage). Outcomes -> edge_avenues.db
paper_outcomes(kind='smart_follow') so agent_lifecycle hires/fires it like every other roster agent.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from .sqlite_util import connect as sqlite_connect

FEE = 0.0125          # pump.fun fee per side on the wallet's own trades
COST_SIDE = 0.02      # our paper cost per side (fee + slippage)
MIN_N = int(os.getenv("NOEMA_SMART_MIN_N", "8"))
SNIPE_S = 2.0
TP, SL, MAX_HOLD_S, MAX_AGE_S, LAT_S = 0.40, -0.25, 1200, 900, 2.0
MCAP_MIN, MCAP_MAX = 15.0, 500.0
NOTIONAL_USD = 5.0
WATCH_CAP = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_trades (signature TEXT, wallet TEXT, mint TEXT, at TEXT, side TEXT, sol REAL,
  tokens REAL, market_cap_sol REAL, PRIMARY KEY (signature, wallet, mint));
CREATE TABLE IF NOT EXISTS backfilled (wallet TEXT PRIMARY KEY, at TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS watch_wallets (wallet TEXT PRIMARY KEY, why TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS smart_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, report_json TEXT);
"""


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def connect(path: str) -> sqlite3.Connection:
    c = sqlite_connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


# -- loading -------------------------------------------------------------------
def load_trades(main_db: str, smart_db: str | None = None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """All trades (time-ordered, deduped by signature+wallet) and mint meta {created_at, creator}.

    Materialises every trade in memory - fine for tests / small DBs. The live loop uses `scan()`, which streams the
    same rows (same order, same dedupe) straight from SQLite instead."""
    meta = load_meta(main_db)
    return list(iter_trades(main_db, smart_db)), meta


def load_meta(main_db: str) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    m = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=30)
    try:
        for mint, at, creator in m.execute("SELECT mint, received_at, creator FROM launch_stream"):
            meta[sys.intern(mint)] = {"created_at": _ts(at), "creator": creator}
    finally:
        m.close()
    return meta


def _has_table(c: sqlite3.Connection, schema: str, table: str) -> bool:
    return c.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _open_union(main_db: str, smart_db: str | None) -> tuple[sqlite3.Connection, str]:
    """Read-only connection + SQL for the deduped trade union.

    launch_trades rows win over wallet_trades rows with the same (signature, wallet); among wallet_trades duplicates the
    first row (rowid order) wins - exactly the old dict/setdefault semantics."""
    m = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=30)
    m.execute("PRAGMA temp_store=FILE")  # big ORDER BY spills to disk, not RAM
    sql = ("SELECT 0 AS src, l.rowid AS rid, l.signature AS signature, l.trader AS wallet, l.mint AS mint, "
           "l.received_at AS at, l.side AS side, l.sol AS sol, l.tokens AS tokens, l.market_cap_sol AS mcap "
           "FROM main.launch_trades l WHERE l.trader IS NOT NULL")
    if smart_db and os.path.isfile(smart_db):
        m.execute("ATTACH DATABASE ? AS sw", (f"file:{smart_db}?mode=ro",))
        if _has_table(m, "sw", "wallet_trades"):
            sql += (" UNION ALL SELECT 1, w.rowid, w.signature, w.wallet, w.mint, w.at, w.side, w.sol, w.tokens, "
                    "w.market_cap_sol FROM sw.wallet_trades w "
                    "WHERE NOT EXISTS (SELECT 1 FROM main.launch_trades l2 WHERE l2.signature = w.signature "
                    "AND l2.trader = w.wallet) "
                    "AND NOT EXISTS (SELECT 1 FROM sw.wallet_trades w2 WHERE w2.signature = w.signature "
                    "AND w2.wallet = w.wallet AND w2.rowid < w.rowid)")
    return m, sql


def iter_trades(main_db: str, smart_db: str | None = None) -> Iterator[dict[str, Any]]:
    """Stream deduped trades in time order (ties: launch_trades first, then insertion order) without holding them."""
    m, sql = _open_union(main_db, smart_db)
    intern = sys.intern
    try:
        cur = m.execute(f"SELECT src, signature, wallet, mint, at, side, sol, tokens, mcap FROM ({sql}) "
                        "ORDER BY at, src, rid")
        for src, sig, w, mint, at, side, sol, tok, mc in cur:
            yield {"sig": sig.split(":")[0] if src == 0 else sig, "wallet": intern(w), "mint": intern(mint),
                   "t": _ts(at), "side": side, "sol": float(sol or 0), "tokens": float(tok or 0), "mcap": mc}
    finally:
        m.close()


def bundled_sigs(main_db: str, smart_db: str | None = None) -> set[str]:
    """Buy signatures shared by >1 wallet (bundles), computed in SQLite instead of a per-signature Python map."""
    m, sql = _open_union(main_db, smart_db)
    try:
        return {r[0] for r in m.execute(
            "SELECT sig FROM (SELECT CASE WHEN src = 0 AND instr(signature, ':') > 0 "
            "THEN substr(signature, 1, instr(signature, ':') - 1) ELSE signature END AS sig, wallet "
            f"FROM ({sql}) WHERE side = 'buy') GROUP BY sig HAVING COUNT(DISTINCT wallet) > 1")}
    finally:
        m.close()


# -- positions -----------------------------------------------------------------
class Pos:
    """One wallet x mint position. Slotted (a dict per position was most of the old 2.5 GB); supports p["key"] and
    p.get("key") so existing callers keep working. Unset optional fields (first_size_sol, pnl_sol, ...) read as
    missing, like the old dict keys."""

    __slots__ = (
        "bought",
        "bundled",
        "close_at",
        "cost",
        "dev",
        "entry_age_s",
        "entry_at",
        "entry_mcap",
        "first_size_sol",
        "hold_s",
        "mint",
        "n_sells",
        "partial",
        "pnl_sol",
        "proceeds",
        "ret",
        "snipe",
        "sold",
        "wallet",
    )

    def __init__(self, wallet: str, mint: str, entry_at: float, entry_mcap: Any, entry_age_s: float | None,
                 dev: bool, snipe: bool, bundled: bool) -> None:
        self.wallet, self.mint, self.entry_at, self.entry_mcap = wallet, mint, entry_at, entry_mcap
        self.entry_age_s, self.dev, self.snipe, self.bundled = entry_age_s, dev, snipe, bundled
        self.cost = self.bought = self.proceeds = self.sold = 0.0
        self.n_sells = 0
        self.close_at: float | None = None

    def __getitem__(self, k: str) -> Any:
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k) from None

    def __setitem__(self, k: str, v: Any) -> None:
        setattr(self, k, v)

    def __contains__(self, k: str) -> bool:
        return hasattr(self, k)

    def get(self, k: str, default: Any = None) -> Any:
        return getattr(self, k, default)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__ if hasattr(self, k)}


class PositionBook:
    """Incremental position builder: feed trades in time order; positions/by_wallet are always 'as of the last trade'."""

    def __init__(self, meta: dict[str, dict[str, Any]], is_bundled: Callable[[str], bool]) -> None:
        self.meta, self.is_bundled = meta, is_bundled
        self._pos: dict[tuple[str, str], Pos] = {}
        self.positions: list[Pos] = []
        self.by_wallet: dict[str, list[Pos]] = {}

    def add(self, t: dict[str, Any]) -> None:
        k = (t["wallet"], t["mint"])
        p = self._pos.get(k)
        if p is None:
            if t["side"] != "buy":
                return  # sells of a position opened before we saw it: unknown cost basis, skip
            mm = self.meta.get(t["mint"]) or {}
            age = t["t"] - mm["created_at"] if mm.get("created_at") else None
            p = self._pos[k] = Pos(t["wallet"], t["mint"], t["t"], t["mcap"], age,
                                   bool(mm.get("creator")) and mm.get("creator") == t["wallet"],
                                   age is not None and age < SNIPE_S, self.is_bundled(t["sig"]))
            self.positions.append(p)
            self.by_wallet.setdefault(t["wallet"], []).append(p)
        if p.close_at is not None:
            return
        if t["side"] == "buy":
            p.cost += t["sol"] * (1 + FEE)
            p.bought += t["tokens"]
            if not hasattr(p, "first_size_sol"):
                p.first_size_sol = t["sol"]
        else:
            p.proceeds += t["sol"] * (1 - FEE)
            p.sold += t["tokens"]
            p.n_sells += 1
            if p.bought > 0 and p.sold >= 0.95 * p.bought:
                p.close_at = t["t"]
                p.pnl_sol = p.proceeds - p.cost
                p.ret = p.pnl_sol / p.cost if p.cost else 0.0
                p.hold_s = t["t"] - p.entry_at
                p.partial = p.n_sells > 1


def build_positions(trades: Iterable[dict[str, Any]], meta: dict[str, dict[str, Any]]) -> list[Pos]:
    trades = trades if isinstance(trades, list) else list(trades)
    by_sig: dict[str, set[str]] = defaultdict(set)
    for t in trades:
        if t["side"] == "buy":
            by_sig[t["sig"]].add(t["wallet"])
    book = PositionBook(meta, lambda s: len(by_sig.get(s, ())) > 1)
    for t in trades:
        book.add(t)
    return book.positions


def bootstrap_lo(xs: list[float], n: int = 1000, seed: int = 7) -> float:
    rng = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)]


_CI_WHY = "bootstrap 95% lower bound <= 0"


def _stat(ps: list[Pos], asof: float, lazy_ci: bool = False) -> dict[str, Any]:
    """Stats of one wallet from its positions entered before `asof` (caller filters). With lazy_ci the bootstrap only
    runs when every other vetting gate already passed - the outcome (vetted / why) is identical, ci_lo is just left
    None for wallets the bootstrap could not change."""
    closed = [p for p in ps if p.close_at is not None and p.close_at < asof]
    old = [p for p in ps if p.entry_at < asof - 7200]
    old_open = [p for p in old if p.close_at is None or p.close_at >= asof]
    pnl = [p.pnl_sol for p in closed]
    n = len(ps)
    st = {"n_entries": n, "n_closed": len(closed), "win_rate": sum(x > 0 for x in pnl) / len(pnl) if pnl else None,
          "pnl_sol": round(sum(pnl), 4), "pnl_ex_best": round(sum(pnl) - max(pnl), 4) if pnl else None,
          "dev_share": sum(p.dev for p in ps) / n, "snipe_share": sum(p.snipe for p in ps) / n,
          "bundle_share": sum(p.bundled for p in ps) / n,
          "open_ratio": len(old_open) / len(old) if old else 0.0, "ci_lo": None}
    if len(pnl) >= MIN_N and not lazy_ci:
        st["ci_lo"] = round(bootstrap_lo(pnl), 5)
    st["vetted"], st["why"] = _vet(st)
    if lazy_ci and len(pnl) >= MIN_N and st["why"] == _CI_WHY:
        st["ci_lo"] = round(bootstrap_lo(pnl), 5)
        st["vetted"], st["why"] = _vet(st)
    return st


def wallet_stats(positions: Iterable[Pos], asof: float, lazy_ci: bool = False) -> dict[str, dict[str, Any]]:
    """Per-wallet stats using ONLY information available at `asof` (closes before asof, entries before asof)."""
    by: dict[str, list[Pos]] = defaultdict(list)
    for p in positions:
        if p.entry_at < asof:
            by[p.wallet].append(p)
    return {w: _stat(ps, asof, lazy_ci) for w, ps in by.items()}


def _vet(st: dict[str, Any]) -> tuple[bool, str]:
    if st["n_closed"] < MIN_N:
        return False, f"n_closed {st['n_closed']} < {MIN_N}"
    if st["dev_share"] > 0:
        return False, "dev/creator wallet"
    if st["snipe_share"] > 0.5:
        return False, "sniper (most entries < 2s after create)"
    if st["bundle_share"] > 0.3:
        return False, "bundler (shares buy txs)"
    if st["open_ratio"] > 0.5:
        return False, "bag-holder (most older entries never closed)"
    if (st["win_rate"] or 0) < 0.5:
        return False, "win rate < 50%"
    if (st["pnl_ex_best"] or 0) <= 0:
        return False, "one lucky hit (PnL <= 0 without best trade)"
    if (st["ci_lo"] or 0) <= 0:
        return False, "bootstrap 95% lower bound <= 0"
    return True, "vetted"


def patterns(positions: list[dict[str, Any]], wallets: set[str]) -> dict[str, Any]:
    ps = [p for p in positions if p["wallet"] in wallets and p["close_at"] is not None]

    def q(xs: list[float]) -> dict[str, Any]:
        xs = sorted(x for x in xs if x is not None)
        return {"n": len(xs), "p25": xs[len(xs) // 4] if xs else None, "p50": xs[len(xs) // 2] if xs else None,
                "p75": xs[3 * len(xs) // 4] if xs else None}
    return {"entry_age_s": q([p["entry_age_s"] for p in ps]), "entry_mcap_sol": q([p["entry_mcap"] for p in ps]),
            "size_sol": q([p.get("first_size_sol") for p in ps]), "hold_s": q([p["hold_s"] for p in ps]),
            "partial_sell_share": round(sum(p["partial"] for p in ps) / len(ps), 3) if ps else None,
            "win_ret": q([p["ret"] for p in ps if p["ret"] > 0]), "loss_ret": q([p["ret"] for p in ps if p["ret"] <= 0])}


# -- SMART-FOLLOW paper eval (no look-ahead) --------------------------------------
class FollowEval:
    """Online SMART-FOLLOW evaluator. Feed trades in time order *before* adding them to the PositionBook whose
    `by_wallet` it reads; vetting uses the hour-boundary snapshot before each signal (memoised per wallet for the
    current hour - positions entered/closed after the boundary never change that snapshot, so it is exact)."""

    def __init__(self, by_wallet: dict[str, list[Pos]], meta: dict[str, dict[str, Any]], now: float,
                 lazy_ci: bool = True) -> None:
        self.by_wallet, self.meta, self.now, self.lazy_ci = by_wallet, meta, now, lazy_ci
        self.seen: set[str] = set()
        self.pending: dict[str, dict[str, Any]] = {}
        self.done: list[tuple[int, dict[str, Any]]] = []
        self.hour: int | None = None
        self.memo: dict[str, dict[str, Any] | None] = {}
        self.seq = 0

    def _wallet(self, w: str, hour: int) -> dict[str, Any] | None:
        if hour != self.hour:
            self.hour, self.memo = hour, {}
        if w not in self.memo:
            asof = hour * 3600.0
            ps = [p for p in self.by_wallet.get(w, ()) if p.entry_at < asof]
            self.memo[w] = _stat(ps, asof, self.lazy_ci) if ps else None
        return self.memo[w]

    def on_trade(self, t: dict[str, Any]) -> None:
        m = t["mint"]
        pd = self.pending.get(m)
        if pd is not None and t.get("mcap"):
            self._feed(m, pd, t)
        if t["side"] != "buy" or m in self.seen:
            return
        mm = self.meta.get(m) or {}
        if not mm.get("created_at") or t["t"] - mm["created_at"] > MAX_AGE_S or mm.get("creator") == t["wallet"]:
            return
        st = self._wallet(t["wallet"], int(t["t"] // 3600))
        if not st or not st["vetted"]:
            return
        self.seen.add(m)
        self.pending[m] = {"seq": self.seq, "t": t, "mm": mm, "st": st, "e": None, "last": None}
        self.seq += 1

    def _feed(self, m: str, pd: dict[str, Any], x: dict[str, Any]) -> None:
        e = pd["e"]
        if e is None:
            if x["t"] < pd["t"]["t"] + LAT_S:
                return
            pd["e"] = pd["last"] = x
            if not (MCAP_MIN <= x["mcap"] <= MCAP_MAX):
                del self.pending[m]
            return
        if x["t"] - e["t"] > MAX_HOLD_S:
            self._finish(m, pd, pd["last"], "time")
            return
        r = x["mcap"] / e["mcap"] - 1
        if r >= TP or r <= SL or (x["wallet"] == pd["t"]["wallet"] and x["side"] == "sell"):
            self._finish(m, pd, x, "tp" if r >= TP else "sl" if r <= SL else "wallet_sold")
            return
        pd["last"] = x

    def _finish(self, m: str, pd: dict[str, Any], x: dict[str, Any], why: str) -> None:
        del self.pending[m]
        e, t, mm, st = pd["e"], pd["t"], pd["mm"], pd["st"]
        if why == "time" and self.now - e["t"] < MAX_HOLD_S:
            return  # still open -> not scored yet
        ret = x["mcap"] / e["mcap"] - 1 - 2 * COST_SIDE
        self.done.append((pd["seq"], {
            "id": f"sf:{t['mint']}", "at": _iso(e["t"]), "wallet": t["wallet"], "mint": t["mint"],
            "entry_mcap": e["mcap"], "exit_mcap": x["mcap"], "exit": why, "hold_s": round(x["t"] - e["t"], 1),
            "ret": round(ret, 4), "net": round(NOTIONAL_USD * ret, 4),
            "x": {"entry_age_s": round(t["t"] - mm["created_at"], 1), "entry_mcap": e["mcap"],
                  "wallet_ci_lo": st["ci_lo"], "wallet_n": st["n_closed"]}}))

    def finish(self) -> list[dict[str, Any]]:
        for m, pd in list(self.pending.items()):
            if pd["e"] is None:
                del self.pending[m]
            else:
                self._finish(m, pd, pd["last"], "time")
        return [r for _, r in sorted(self.done, key=lambda d: d[0])]


def follow_signals(trades: Iterable[dict[str, Any]], positions: Iterable[Pos], meta: dict[str, dict[str, Any]],
                   now: float) -> list[dict[str, Any]]:
    by_wallet: dict[str, list[Pos]] = defaultdict(list)
    for p in positions:
        by_wallet[p.wallet].append(p)
    fe = FollowEval(by_wallet, meta, now)
    for t in trades:
        fe.on_trade(t)
    return fe.finish()


def smart_features(positions: list[dict[str, Any]], mint: str, asof: float,
                   vetted: dict[str, dict[str, Any]] | None = None) -> dict[str, float]:
    """Features for meta-launch / timing agents: vetted (as of the previous hour) wallets that bought `mint` before asof."""
    vetted = vetted if vetted is not None else wallet_stats(positions, (asof // 3600) * 3600)
    buys = [p for p in positions if p["mint"] == mint and p["entry_at"] < asof and (vetted.get(p["wallet"]) or {}).get("vetted")]
    return {"smart_buyers": float(len(buys)), "smart_buy_sol": round(sum(p.get("first_size_sol") or 0 for p in buys), 4)}


# -- Helius backfill (free tier: <= ~3 req/s, capped per cycle) ----------------------
def _rpc(method: str, params: list[Any]) -> Any:
    import urllib.request
    key = os.getenv("HELIUS_API_KEY")
    if not key:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(f"https://mainnet.helius-rpc.com/?api-key={key}", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("result")


def parse_tx_trades(tx: dict[str, Any], wallet: str, sig: str) -> list[dict[str, Any]]:
    import base64

    from .launch_stream import decode_event
    out = []
    for ln in ((tx.get("meta") or {}).get("logMessages") or []):
        if not ln.startswith("Program data: "):
            continue
        try:
            ev = decode_event(base64.b64decode(ln[14:]))
        except ValueError:
            continue
        if ev and ev["kind"] == "trade" and ev["trader"] == wallet:
            out.append({"sig": sig, "wallet": wallet, "mint": ev["mint"], "t": float(tx.get("blockTime") or 0),
                        "side": ev["side"], "sol": ev["sol"], "tokens": ev["tokens"], "mcap": ev["market_cap_sol"]})
    return out


def backfill(c: sqlite3.Connection, wallets: list[str], max_tx: int = 400, per_wallet: int = 60) -> int:
    n_calls, n = 0, 0
    for w in wallets:
        if n_calls >= max_tx:
            break
        try:
            sigs = _rpc("getSignaturesForAddress", [w, {"limit": per_wallet}]) or []
            n_calls += 1
            got = 0
            for s in sigs:
                if s.get("err") is not None or n_calls >= max_tx:
                    continue
                time.sleep(0.35)
                tx = _rpc("getTransaction", [s["signature"], {"maxSupportedTransactionVersion": 0}])
                n_calls += 1
                for t in parse_tx_trades(tx or {}, w, s["signature"]):
                    c.execute("INSERT OR IGNORE INTO wallet_trades VALUES (?,?,?,?,?,?,?,?)",
                              (t["sig"], w, t["mint"], _iso(t["t"]), t["side"], t["sol"], t["tokens"], t["mcap"]))
                    got += 1
            c.execute("INSERT OR REPLACE INTO backfilled VALUES (?,?,?)", (w, datetime.now(UTC).isoformat(), got))
            c.commit()
            n += got
        except Exception as exc:  # noqa: BLE001 - never print the URL (holds the key)
            print(datetime.now(UTC).isoformat(), "backfill", type(exc).__name__, flush=True)
            time.sleep(5)
    return n


def candidates(positions: Iterable[Pos], k: int = WATCH_CAP) -> list[str]:
    # one position per (wallet, mint) -> position count == distinct mints
    cnt: dict[str, int] = {}
    bad: set[str] = set()
    for p in positions:
        cnt[p.wallet] = cnt.get(p.wallet, 0) + 1
        if p.dev:
            bad.add(p.wallet)
    ranked = sorted((w for w, n in cnt.items() if w not in bad and n >= 5), key=lambda w: -cnt[w])
    return ranked[:k]


# -- cycle -----------------------------------------------------------------------
class Scan:
    """One streaming pass over all trades: positions, SMART-FOLLOW signals and the last 30 min of trades, without ever
    holding the full trade list (the old load_trades + build_positions peaked at ~2.5 GB on a 2M-trade DB)."""

    __slots__ = ("meta", "n_trades", "positions", "recent", "signals")

    def __init__(self, main_db: str, smart_db: str | None, now: float, recent_s: float = 1800) -> None:
        self.meta = load_meta(main_db)
        bundled = bundled_sigs(main_db, smart_db)
        book = PositionBook(self.meta, bundled.__contains__)
        fe = FollowEval(book.by_wallet, self.meta, now)
        self.recent: list[dict[str, Any]] = []
        self.n_trades = 0
        since = now - recent_s
        for t in iter_trades(main_db, smart_db):
            self.n_trades += 1
            fe.on_trade(t)  # before book.add: the signal snapshot only uses positions entered before the hour mark
            book.add(t)
            if t["t"] >= since:
                self.recent.append(t)
        self.signals = fe.finish()
        self.positions = book.positions


def cycle(main_db: str, smart_db: str, avenues_db: str | None, *, do_backfill: bool = True) -> dict[str, Any]:
    now = time.time()
    c = connect(smart_db)
    try:
        sc = Scan(main_db, smart_db, now)
        st = wallet_stats(sc.positions, now, lazy_ci=True)
        vetted = {w for w, s in st.items() if s["vetted"]}
        cand = candidates(sc.positions)
        if do_backfill and os.getenv("HELIUS_API_KEY"):
            day = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            done = {r[0] for r in c.execute("SELECT wallet FROM backfilled WHERE at>=?", (day,))}
            todo = [w for w in cand if w not in done][:8]
            if todo:
                backfill(c, todo)
                del sc, st  # free the first pass before rebuilding (never hold two copies)
                sc = Scan(main_db, smart_db, now)
                st = wallet_stats(sc.positions, now, lazy_ci=True)
                vetted = {w for w, s in st.items() if s["vetted"]}
        pos, sig = sc.positions, sc.signals
        watch = list(vetted) + [w for w in cand if w not in vetted]
        c.execute("DELETE FROM watch_wallets")
        c.executemany("INSERT INTO watch_wallets VALUES (?,?,?)",
                      [(w, "vetted" if w in vetted else "candidate", _iso(now)) for w in watch[:WATCH_CAP]])
        if avenues_db:
            a = sqlite_connect(avenues_db)
            a.execute("CREATE TABLE IF NOT EXISTS paper_outcomes (kind TEXT, id TEXT, at TEXT, net REAL, x_json TEXT, "
                      "PRIMARY KEY (kind, id))")
            a.executemany("INSERT OR IGNORE INTO paper_outcomes VALUES (?,?,?,?,?)",
                          [("smart_follow", s["id"], s["at"], s["net"], json.dumps(s["x"])) for s in sig])
            a.commit()
            a.close()
        rets = [s["net"] for s in sig]
        n_closed = sum(p.close_at is not None for p in pos)
        report = {"at": _iso(now), "mode": "paper/read-only", "trades": sc.n_trades, "wallets": len(st),
                  "positions": len(pos), "closed_positions": n_closed, "candidates": len(cand),
                  "vetted": sorted(({"wallet": w, **st[w]} for w in vetted), key=lambda r: -(r["ci_lo"] or 0))[:25],
                  "rejections": _rej_counts(st), "patterns_vetted": patterns(pos, vetted),
                  "patterns_all_closed": patterns(pos, set(st)),
                  "smart_follow": {"n": len(sig), "mean_net_usd": round(sum(rets) / len(rets), 4) if rets else None,
                                   "ci95_lo_usd": round(bootstrap_lo(rets), 4) if len(rets) >= 5 else None,
                                   "win_rate": round(sum(r > 0 for r in rets) / len(rets), 3) if rets else None,
                                   "recent": sig[-10:]},
                  "recent_moves": _recent_moves(sc.recent, vetted | set(cand[:50]), now, vetted)}
        c.execute("INSERT INTO smart_reports (at, report_json) VALUES (?,?)", (report["at"], json.dumps(report)))
        c.execute("DELETE FROM smart_reports WHERE id NOT IN (SELECT id FROM smart_reports ORDER BY id DESC LIMIT 48)")
        c.commit()
        return report
    finally:
        c.close()


def _rej_counts(st: dict[str, dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for s in st.values():
        out[s["why"].split(" (")[0] if not s["why"].startswith("n_closed") else "too few closed positions"] += 1
    return dict(out)


def _recent_moves(trades: list[dict[str, Any]], wallets: set[str], now: float, vetted: set[str]) -> list[dict[str, Any]]:
    out = [{"at": _iso(t["t"]), "wallet": t["wallet"], "vetted": t["wallet"] in vetted, "mint": t["mint"],
            "side": t["side"], "sol": round(t["sol"], 4), "mcap_sol": t["mcap"]}
           for t in trades if t["wallet"] in wallets and t["t"] >= now - 1800]
    return out[-40:][::-1]


def latest_report(smart_db: str) -> dict[str, Any] | None:
    if not os.path.isfile(smart_db):
        return None
    c = sqlite3.connect(f"file:{smart_db}?mode=ro", uri=True, timeout=10)
    try:
        r = c.execute("SELECT report_json FROM smart_reports ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(r[0]) if r else None
    except sqlite3.Error:
        return None
    finally:
        c.close()


def load_watch(smart_db: str) -> dict[str, str]:
    if not os.path.isfile(smart_db):
        return {}
    try:
        c = sqlite3.connect(f"file:{smart_db}?mode=ro", uri=True, timeout=10)
        try:
            return dict(c.execute("SELECT wallet, why FROM watch_wallets"))
        finally:
            c.close()
    except sqlite3.Error:
        return {}


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--smart-db", default="data/smart_wallets.db")
    ap.add_argument("--avenues-db", default="data/edge_avenues.db")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--no-backfill", action="store_true")
    a = ap.parse_args()
    while True:
        try:
            r = cycle(a.db, a.smart_db, a.avenues_db, do_backfill=not a.no_backfill)
            print(r["at"], json.dumps({k: r[k] for k in ("trades", "wallets", "closed_positions", "candidates")}),
                  "vetted", len(r["vetted"]), "smart_follow", json.dumps({k: v for k, v in r["smart_follow"].items()
                                                                          if k != "recent"}), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(datetime.now(UTC).isoformat(), "smart_wallets", type(exc).__name__, str(exc)[:160], flush=True)
        if not a.loop:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
