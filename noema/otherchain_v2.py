"""OTHER CHAINS v2: a separate, newly pre-registered paper arm for the FUNDED chains Base and Robinhood.

PAPER ONLY. Never signs, swaps, bridges or moves funds; never imports the signer, never reads keys or prints
wallet addresses. v1 (``noema.other_chains``) keeps running untouched with its frozen registration; v2 has its
own DB (``other_chains_v2.db``), its own registration row (timestamp + sha256) and its own verdict.

What v2 changes vs v1 (and nothing else; same pass bar, same costs, same paper rule):
  1. Pricing fallback. When DexScreener has no listing for the pair, vetting and every follow-up snapshot use
     GeckoTerminal (``/networks/{net}/pools/multi/{addrs}``: price, reserve, m5 buys/sells/volume) instead of
     skipping as ``not_indexed``. GeckoTerminal snapshots are taken at most once a minute per chain and v2 is
     capped at 4 GT calls/min (timing 10/min + v1 discovery 4/min keep the shared free ~30/min budget).
  2. Tax unknown. When GoPlus has no buy/sell tax yet, v2 asks a second free simulator (honeypot.is buy+sell
     simulation; Base only, it does not support Robinhood) and re-checks GoPlus 30, 60 and 120 s after the first
     unknown. A pair is followed only once both taxes are known, it is not a honeypot and it is buyable and
     sellable. Every second spent waiting is charged as entry lag: the signal can only fire after the pair is
     followed, so the wait delays every possible entry (reported as ``vet_wait_s`` / ``entry_lag_s``).
Discovery reuses v1's discovery feed read-only (no extra API calls): every Base/Robinhood pair v1 records is
picked up within ~15 s and vetted independently by v2.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import statistics
import time
from typing import Any

from . import other_chains as v1
from .other_chains import (
    CHAINS,
    FOLLOW_S,
    GT,
    KYBER_ROUTER,
    MAX_RT_COST,
    MAX_TAX,
    MIN_LIQ_USD,
    NATIVE,
    PAPER_SIZE_USD,
    SNAP_EVERY_S,
    SNAP_RETENTION_DAYS,
    Limiter,
    _f,
    _iso,
    _now,
    security_verdict,
    verdict_for,
)

log = logging.getLogger("noema.otherchain_v2")

HEARTBEAT = "feed:other_chains_v2"
V2_CHAINS = ("base", "robinhood")
GT_SNAP_EVERY_S = 60               # GeckoTerminal follow-up snapshot cadence per chain (free-tier budget)
GT_PER_MIN = 4                     # v2's own GeckoTerminal cap
TAX_RECHECK_S = (30, 60, 120)      # GoPlus re-checks after the first unknown tax
HONEYPOT = "https://api.honeypot.is/v2/IsHoneypot"
HONEYPOT_CHAIN_IDS = {"base": 8453}  # honeypot.is answers "Invalid chain" for Robinhood
V1_FEED_EVERY_S = 15

PREREG_V2: dict[str, Any] = {
    **v1.PREREG,
    "study": "other_chains_v2",
    "chains": list(V2_CHAINS),
    "relation_to_v1": "separate arm; v1 keeps its own frozen registration and verdict; v2 never edits v1 data",
    "rule": {**v1.PREREG["rule"], "gt_snap_every_s": GT_SNAP_EVERY_S, "tax_recheck_s": list(TAX_RECHECK_S)},
    "changes_vs_v1": [
        "price/follow fallback to GeckoTerminal pools when DexScreener has no listing",
        ("tax unknown -> honeypot.is buy+sell simulation (Base) + GoPlus re-checks at 30/60/120 s; follow only once "
         "both taxes are known and the token is buyable and sellable"),
        "the vetting wait is charged as entry lag (no signal before the pair is followed)",
    ],
    "costs": v1.PREREG["costs"] + "; tax from GoPlus, else the honeypot.is simulation",
    "bonferroni_k": len(V2_CHAINS),
    "decision_point": "per chain, the first UTC day end at which min trades, min days and min held-out are all met; "
                      "the verdict then is final for v2 (no waiting for a better one)",
}

V2_COLUMNS = {"price_source": "TEXT", "tax_source": "TEXT", "tax_unknown_at": "REAL", "followed_at": "REAL",
              "vet_wait_s": "REAL"}


def connect(path: str) -> sqlite3.Connection:
    c = v1.connect(path)
    _add_columns(c)
    return c


def _add_columns(c: sqlite3.Connection) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(oc_pairs)")}
    for col, typ in V2_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE oc_pairs ADD COLUMN {col} {typ}")
    c.commit()


def preregister(c: sqlite3.Connection, now: float | None = None) -> str:
    blob = json.dumps(PREREG_V2, sort_keys=True)
    sha = hashlib.sha256(blob.encode()).hexdigest()
    c.execute("INSERT OR IGNORE INTO oc_preregistration(created_at, sha256, criteria_json) VALUES(?,?,?)",
              (_iso(now or _now()), sha, blob))
    c.commit()
    return sha


def next_tax_check(elapsed_s: float) -> float | None:
    """Seconds until the next GoPlus re-check, given time since the first unknown; None = window exhausted."""
    for t in TAX_RECHECK_S:
        if t > elapsed_s + 0.5:
            return t - elapsed_s
    return None


def honeypot_verdict(d: dict[str, Any] | None) -> tuple[str | None, float | None, float | None]:
    """(skip reason or None, buy_tax, sell_tax) from honeypot.is; taxes None = still unknown."""
    if not d:
        return None, None, None
    if (d.get("honeypotResult") or {}).get("isHoneypot") is True:
        return "honeypot", None, None
    sim = d.get("simulationResult") or {}
    if not d.get("simulationSuccess") or sim.get("buyTax") is None or sim.get("sellTax") is None:
        return None, None, None
    bt, st = (_f(sim.get("buyTax")) or 0.0) / 100, (_f(sim.get("sellTax")) or 0.0) / 100
    tt = (_f(sim.get("transferTax")) or 0.0) / 100
    st = max(st, tt)
    if bt > MAX_TAX or st > MAX_TAX:
        return "high_tax", bt, st
    return None, bt, st


def gt_snapshot(p: dict[str, Any], token: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """(token address, snapshot) for one GeckoTerminal pool, priced in the token we follow."""
    a = p.get("attributes") or {}
    rel = p.get("relationships") or {}
    base = str(((rel.get("base_token") or {}).get("data") or {}).get("id") or "").split("_", 1)[-1].lower()
    quote = str(((rel.get("quote_token") or {}).get("data") or {}).get("id") or "").split("_", 1)[-1].lower()
    tok = (token or base or "").lower()
    price = _f(a.get("quote_token_price_usd")) if tok and tok == quote else _f(a.get("base_token_price_usd"))
    if not price:
        return tok or None, None
    m5 = (a.get("transactions") or {}).get("m5") or {}
    return tok or None, {"price": price, "liq": _f(a.get("reserve_in_usd")) or 0.0,
                         "vol_m5": _f((a.get("volume_usd") or {}).get("m5")) or 0.0,
                         "buys_m5": int(m5.get("buys") or 0), "sells_m5": int(m5.get("sells") or 0),
                         "fdv": _f(a.get("fdv_usd"))}


class ScannerV2(v1.Scanner):
    def __init__(self, db: str, main_db: str, v1_db: str, client: Any = None) -> None:
        super().__init__(db, main_db, client, chains={k: CHAINS[k] for k in V2_CHAINS})
        _add_columns(self.c)
        self.v1_db = v1_db
        self.lim["gt"] = Limiter(GT_PER_MIN)
        self.lim["hp"] = Limiter(6)
        self.gt_last: dict[str, float] = {}
        self.cursor = _now() - 120

    def register(self) -> str:
        return preregister(self.c)

    # -- discovery: v1's feed, read-only --
    async def v1_feed_loop(self) -> None:
        q = ",".join("?" * len(V2_CHAINS))
        while True:
            try:
                m = sqlite3.connect(f"file:{self.v1_db}?mode=ro", uri=True, timeout=5)
                rows = m.execute(f"SELECT chain, pair, token, symbol, dex, quote_symbol, source, created_at, seen_at "
                                 f"FROM oc_pairs WHERE chain IN ({q}) AND seen_at > ? ORDER BY seen_at",
                                 (*V2_CHAINS, self.cursor)).fetchall()
                m.close()
                n_new = 0
                for ch, pair, tok, sym, dex, qs, src, created, seen in rows:
                    n_new += self.add_pair(ch, pair, created, src, tok, sym, dex, qs)
                    self.cursor = max(self.cursor, seen)
                self.c.commit()
                self.poll("*", "v1_feed", True, len(rows), n_new)
            except Exception as exc:  # noqa: BLE001
                self.poll("*", "v1_feed", False, err=exc)
            await asyncio.sleep(V1_FEED_EVERY_S)

    # -- GeckoTerminal pools --
    async def gt_pools(self, chain: str, pairs: list[str], source: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(pairs), 30):
            try:
                d = await self.get("gt", f"{GT}/networks/{CHAINS[chain].gt}/pools/multi/{','.join(pairs[i:i + 30])}")
                for p in (d or {}).get("data") or []:
                    out[str((p.get("attributes") or {}).get("address") or "").lower()] = p
                self.poll(chain, source, True, len(pairs[i:i + 30]), len(out))
            except Exception as exc:
                self.poll(chain, source, False, err=exc)
                raise
        return out

    async def honeypot(self, chain: str, token: str) -> dict[str, Any] | None:
        cid = HONEYPOT_CHAIN_IDS.get(chain)
        if not cid:
            return None
        try:
            return await self.get("hp", HONEYPOT, params={"address": token, "chainID": cid})
        except Exception as exc:  # noqa: BLE001
            self.errors["honeypot"] = str(exc)[:120]
            return None

    # -- vetting --
    async def vet(self, chain: str, pair: str) -> None:
        ch = CHAINS[chain]
        row = self.c.execute("SELECT * FROM oc_pairs WHERE chain=? AND pair=?", (chain, pair)).fetchone()
        if not row or row["stage"] != "seen":
            return
        if _now() - row["seen_at"] > 600:
            self.skip(chain, pair, "queue_timeout")
            return
        p = (await self.ds_pairs(chain, [pair])).get(pair)
        src = "dexscreener"
        if p:
            liq = _f((p.get("liquidity") or {}).get("usd")) or 0.0
            token = str((p.get("baseToken") or {}).get("address") or row["token"] or "").lower()
            meta = ((p.get("baseToken") or {}).get("symbol"), p.get("dexId"), (p.get("quoteToken") or {}).get("symbol"))
        else:
            g = (await self.gt_pools(chain, [pair], "gt_vet")).get(pair)
            tok, snap = gt_snapshot(g, row["token"]) if g else (None, None)
            if not snap or not tok:
                tries = (row["tries"] or 0) + 1
                if tries >= 4:
                    self.skip(chain, pair, "not_indexed")
                else:
                    self.c.execute("UPDATE oc_pairs SET tries=? WHERE chain=? AND pair=?", (tries, chain, pair))
                    asyncio.get_running_loop().call_later(30, self.vet_q.append, (chain, pair))
                return
            src, liq, token, meta = "geckoterminal", snap["liq"], tok, (row["symbol"], row["dex"], row["quote_symbol"])
        self.c.execute("UPDATE oc_pairs SET liq_seen=?, token=?, symbol=coalesce(symbol,?), dex=coalesce(?,dex), "
                       "quote_symbol=coalesce(?,quote_symbol), price_source=? WHERE chain=? AND pair=?",
                       (liq, token, *meta, src, chain, pair))
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
        reason, bt, st = security_verdict(await self.goplus(ch, token))
        tax_src = "goplus"
        if reason in ("security_unknown", "tax_unknown"):
            hr, hbt, hst = honeypot_verdict(await self.honeypot(chain, token))
            if hr:
                reason, bt, st, tax_src = hr, hbt or 0.0, hst or 0.0, "honeypot.is"
            elif hbt is not None and hst is not None:
                reason, bt, st, tax_src = None, hbt, hst, "honeypot.is"
        if reason in ("security_unknown", "tax_unknown"):
            now = _now()
            first = row["tax_unknown_at"] or now
            wait = next_tax_check(now - first)
            if wait is not None:
                self.c.execute("UPDATE oc_pairs SET tax_unknown_at=? WHERE chain=? AND pair=?", (first, chain, pair))
                asyncio.get_running_loop().call_later(wait, self.vet_q.append, (chain, pair))
                return
        if reason:
            self.skip(chain, pair, reason, buy_tax=bt, sell_tax=st, tax_source=tax_src)
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
        self.c.execute("UPDATE oc_pairs SET stage='followed', buy_tax=?, sell_tax=?, tax_source=?, funded=?, bridge_s=?, "
                       "bridge_cost_usd=?, follow_until=?, followed_at=?, vet_wait_s=?, updated_at=? "
                       "WHERE chain=? AND pair=?",
                       (bt, st, tax_src, int(funded), bridge_s, bridge_cost, now + FOLLOW_S, now, now - row["seen_at"],
                        now, chain, pair))

    # -- follow + paper (DexScreener first, GeckoTerminal for unlisted pairs) --
    async def follow_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            by: dict[str, list[sqlite3.Row]] = {}
            for r in self.c.execute("SELECT * FROM oc_pairs WHERE stage='followed'").fetchall():
                by.setdefault(r["chain"], []).append(r)
            for chain, rs in by.items():
                try:
                    ds_rows = [r for r in rs if r["price_source"] != "geckoterminal"]
                    got = await self.ds_pairs(chain, [r["pair"] for r in ds_rows]) if ds_rows else {}
                    now = _now()
                    gt_rows = [r for r in rs if r["price_source"] == "geckoterminal"]
                    for r in ds_rows:
                        p = got.get(r["pair"])
                        s = None
                        if p and _f(p.get("priceUsd")):
                            m5 = ((p.get("txns") or {}).get("m5") or {})
                            s = {"price": _f(p.get("priceUsd")), "liq": _f((p.get("liquidity") or {}).get("usd")) or 0.0,
                                 "vol_m5": _f((p.get("volume") or {}).get("m5")) or 0.0, "buys_m5": int(m5.get("buys") or 0),
                                 "sells_m5": int(m5.get("sells") or 0), "fdv": _f(p.get("fdv"))}
                            self._snap(chain, r["pair"], now, s)
                        self.step(r, s, now)
                    if gt_rows and time.time() - self.gt_last.get(chain, 0) >= GT_SNAP_EVERY_S - 2:
                        self.gt_last[chain] = time.time()
                        gp = await self.gt_pools(chain, [r["pair"] for r in gt_rows], "gt_follow")
                        now = _now()   # snapshot time = right after the fetch
                        for r in gt_rows:
                            _, s = gt_snapshot(gp[r["pair"]], r["token"]) if r["pair"] in gp else (None, None)
                            if s:
                                self._snap(chain, r["pair"], now, s)
                            self.step(r, s, now)
                    self.c.commit()
                    self.poll(chain, "follow", True, len(rs))
                except Exception as exc:  # noqa: BLE001
                    self.poll(chain, "follow", False, err=exc)   # no fresh data -> no decisions this pass
            await asyncio.sleep(max(1.0, SNAP_EVERY_S - (time.monotonic() - t0)))

    def _snap(self, chain: str, pair: str, now: float, s: dict[str, Any]) -> None:
        self.c.execute("INSERT INTO oc_snaps VALUES(?,?,?,?,?,?,?,?,?)",
                       (chain, pair, now, s["price"], s["liq"], s["vol_m5"], s["buys_m5"], s["sells_m5"], s["fdv"]))

    async def housekeeping(self) -> None:
        from .heartbeats import beat
        while True:
            self.refresh_funded()
            now = _now()
            ok = now - self.last_ok.get("v1_feed", 0) < 120
            beat(HEARTBEAT, ok, None if ok else (self.errors.get("v1_feed") or "v1 discovery feed not readable"),
                 cadence_s=60)
            self.passes += 1
            if self.passes % 2 == 1:
                f = dict(self.c.execute("SELECT stage, count(*) FROM oc_pairs GROUP BY 1").fetchall())
                log.info("v2 funnel %s · queue %d · feed %s", json.dumps(f, sort_keys=True), len(self.vet_q),
                         "ok" if ok else "STALE")
            self.c.execute("DELETE FROM oc_snaps WHERE at < ?", (now - SNAP_RETENTION_DAYS * 86400,))
            self.c.execute("DELETE FROM oc_polls WHERE at < ?", (now - 3 * 86400,))
            self.c.commit()
            await asyncio.sleep(30)

    async def run(self) -> None:
        self.refresh_funded()
        for r in self.c.execute("SELECT chain, pair FROM oc_pairs WHERE stage='seen'"):
            self.vet_q.append((r[0], r[1]))
        await asyncio.gather(self.v1_feed_loop(), self.vet_loop(), self.follow_loop(), self.housekeeping())


def _med(xs: list[float]) -> int | None:
    return round(statistics.median(xs)) if xs else None


def board(db: str) -> dict[str, Any]:
    """Read-only v2 payload: per-chain funnel (seen, skips by reason, followed, entries), lag and verdict."""
    if not os.path.isfile(db):
        return {"status": "not started", "paper_only": True, "study": PREREG_V2["study"]}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        pre = c.execute("SELECT created_at, sha256 FROM oc_preregistration ORDER BY id LIMIT 1").fetchone()
        cols = {r[1] for r in c.execute("PRAGMA table_info(oc_pairs)")}
        chains = []
        for name in V2_CHAINS:
            f = c.execute("SELECT count(*) seen, sum(stage='skipped') skipped, sum(stage IN ('followed','closed')) followed,"
                          " sum(signal_at IS NOT NULL) signals, sum(entry_at IS NOT NULL) entries, max(funded) funded"
                          " FROM oc_pairs WHERE chain=?", (name,)).fetchone()
            skips = {r[0]: r[1] for r in c.execute("SELECT skip_reason, count(*) FROM oc_pairs WHERE chain=? AND "
                                                   "stage='skipped' GROUP BY 1 ORDER BY 2 DESC", (name,))}
            lag = sorted(r[0] for r in c.execute("SELECT lag_s FROM oc_pairs WHERE chain=? AND lag_s IS NOT NULL AND "
                                                 "created_at >= (SELECT min(seen_at) FROM oc_pairs WHERE chain=?)",
                                                 (name, name)))
            extra: dict[str, Any] = {}
            if "followed_at" in cols:
                fol = c.execute("SELECT vet_wait_s, price_source, tax_source, tax_unknown_at, followed_at "
                                "FROM oc_pairs WHERE chain=? AND followed_at IS NOT NULL", (name,)).fetchall()
                ent = [r[0] for r in c.execute("SELECT entry_at - created_at FROM oc_pairs WHERE chain=? AND "
                                               "entry_at IS NOT NULL", (name,))]
                ent_sig = [r[0] for r in c.execute("SELECT entry_at - signal_at FROM oc_pairs WHERE chain=? AND "
                                                   "entry_at IS NOT NULL", (name,))]
                waited = [r["followed_at"] - r["tax_unknown_at"] for r in fol if r["tax_unknown_at"]]
                extra = {
                    "followed_by_price_source": _count(r["price_source"] for r in fol),
                    "followed_by_tax_source": _count(r["tax_source"] for r in fol),
                    "vet_wait_median_s": _med([r["vet_wait_s"] for r in fol if r["vet_wait_s"] is not None]),
                    "tax_resolved_after_wait": len(waited), "tax_wait_median_s": _med(waited),
                    "entry_lag_median_s": _med(ent), "entry_lag_from_signal_median_s": _med(ent_sig),
                    "gt_priced_seen": c.execute("SELECT count(*) FROM oc_pairs WHERE chain=? AND price_source="
                                                "'geckoterminal'", (name,)).fetchone()[0],
                }
            trades = [(r[0], r[1]) for r in c.execute("SELECT day, net_ret FROM oc_pairs WHERE chain=? AND "
                                                       "net_ret IS NOT NULL ORDER BY entry_at", (name,))]
            chains.append({"chain": name, "funded": bool(f["funded"]), "seen": f["seen"] or 0,
                           "skipped": f["skipped"] or 0, "buyable_followed": f["followed"] or 0,
                           "signals": f["signals"] or 0, "entries": f["entries"] or 0, "skips": skips,
                           "lag_median_s": _med(lag), "lag_p10_s": round(lag[len(lag) // 10]) if lag else None,
                           "closed": len(trades), "verdict": verdict_for(trades, len(V2_CHAINS)), **extra})
        last = c.execute("SELECT max(at) FROM oc_polls WHERE ok=1").fetchone()[0]
    finally:
        c.close()
    return {"paper_only": True, "study": PREREG_V2["study"], "preregistered_at": pre["created_at"] if pre else None,
            "prereg_sha256": pre["sha256"] if pre else None, "paper_size_usd": PAPER_SIZE_USD,
            "last_poll_at": _iso(last), "chains": chains}


def _count(xs: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x or "?"] = out.get(x or "?", 0) + 1
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Paper-only other-chain v2 arm (Base + Robinhood; never trades).")
    ap.add_argument("--db", default=os.getenv("NOEMA_OTHERCHAIN_V2_DB", "data/other_chains_v2.db"))
    ap.add_argument("--v1-db", default=os.getenv("NOEMA_OTHERCHAIN_DB", "data/other_chains.db"))
    ap.add_argument("--main-db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--board", action="store_true", help="print the board JSON and exit")
    a = ap.parse_args(argv)
    if a.board:
        print(json.dumps(board(a.db), indent=1, default=str))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("other_chains v2 paper arm starting: chains=%s size=$%s", ",".join(V2_CHAINS), PAPER_SIZE_USD)
    asyncio.run(ScannerV2(a.db, a.main_db, a.v1_db).run())


if __name__ == "__main__":
    main()
