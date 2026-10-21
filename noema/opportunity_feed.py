"""Unified live OPPORTUNITY FEED for the TEV room.

Pulls candidate opportunities from rows NOEMA's crawlers already store (Kalshi/Polymarket
quotes, X bursts, news links, culture radar, alpha screens, trench sweeps, strategy book),
dedupes them by a stable id, remembers when each was FIRST seen, expires items whose source
stopped reporting them, and keeps a short history of what happened afterwards (price move
since found). Nothing is seeded or synthesised: a source with no rows reports ``empty``.

The trading DB is opened read-only. Feed memory (first-seen, reference price, history) lives
in a separate small SQLite file next to it (``NOEMA_OPP_FEED_DB``), so the dashboard never
writes into the trading database. Edge numbers are estimates after fees where a cost model
exists; nothing here is a proven money-maker and nothing here places orders.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import edge_strategies as es

NEW_FOR_S = 15 * 60          # an item is NEW for 15 minutes after first sighting
HISTORY_H = 24               # keep expired finds this long
REFRESH_S = 10.0             # rescan the trading DB at most this often
_LOCK = threading.Lock()
_LAST: dict[str, Any] = {"at": 0.0, "payload": None, "key": None}

SOURCES = {
    "pm_arb": "Kalshi ↔ Polymarket arb / ladder gaps",
    "news_latency": "news & X latency (directional)",
    "x_burst": "X bursts on linked markets",
    "alpha": "alpha screens (rug-checked tokens)",
    "culture": "culture radar (crypto · stocks · prediction buzz)",
    "trench": "pump.fun / DEX pool sweeps",
    "strategy": "strategy book signals",
}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _iso(v: Any) -> str | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace(" ", "T"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).isoformat()


def feed_db_path(db_path: str) -> str:
    return os.getenv("NOEMA_OPP_FEED_DB") or str(Path(db_path).with_name("opportunity_feed.db"))


def _feed_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=5)
    c.execute("""CREATE TABLE IF NOT EXISTS opp_items (
        id TEXT PRIMARY KEY, source TEXT, tier INTEGER, title TEXT, venue TEXT, ref TEXT,
        first_seen TEXT, last_seen TEXT, source_at TEXT, price_kind TEXT, ref_price REAL, last_price REAL,
        edge REAL, score REAL, reason TEXT, url TEXT, status TEXT, expired_at TEXT, payload_json TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS opp_scans (at TEXT, source TEXT, n INTEGER, note TEXT)")
    return c


# ------------------------------------------------------------------ collectors
def _q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _mid(q: dict[str, Any]) -> float | None:
    a, b = _f(q.get("yes_ask")), _f(q.get("yes_bid"))
    return round((a + b) / 2, 4) if a is not None and b is not None else None


def collect(conn: sqlite3.Connection, now: datetime) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (items, per-source status). Each item has a stable ``id``."""
    items: list[dict[str, Any]] = []
    st: dict[str, dict[str, Any]] = {}
    iso_now = now.isoformat()

    def add(source: str, **kw: Any) -> None:
        kw.setdefault("source_at", iso_now)
        items.append({"source": source, **kw})

    # 1) prediction-market structural gaps (locked if resolutions are equivalent)
    try:
        quotes = es._quotes(conn, now, 60)
    except sqlite3.Error:
        quotes = []
    by_q = {(q["venue"], q["market_id"]): q for q in quotes}
    n_k = sum(1 for q in quotes if q["venue"].startswith("kalshi"))
    arbs = es.ladder_arbs(quotes, min_edge=0.0) + es.cross_venue_gaps(quotes, min_edge=0.0)
    for a in arbs:
        cost = _f(a.get("cost")) or 0
        add("pm_arb", id=f"arb:{a['key']}", tier=0, title=a["title"], venue="kalshi+polymarket"
            if a.get("kind") == "cross_venue" else "kalshi", ref=a["key"],
            edge=round((a["edge"] or 0) / cost, 4) if cost else None, score=a["edge"],
            reason=f"{a['kind']}: pay {cost:.3f} for a {a['locked_payoff']:.0f} payoff after fees+slippage"
                   + (" · resolution wording must be checked" if a.get("needs_resolution_review") else ""),
            price_kind="cost", price=cost,
            legs=a.get("legs"))
    st["pm_arb"] = {"n": len(arbs), "note": f"{len(quotes)} two-sided quotes ({n_k} Kalshi, "
                    f"{len(quotes) - n_k} Polymarket US) checked in the last hour"
                    + ("" if arbs else " · no gap clears fees right now")}

    # 2) news / X latency (directional, model-guessed fair value)
    try:
        lat = es.news_latency_signals(conn, quotes, now)
    except sqlite3.Error:
        lat = []
    for s in lat:
        leg = s["legs"][0]
        cost, fair = _f(s.get("cost")), _f(s.get("fair"))
        add("news_latency", id=f"lat:{s['key']}", tier=1, title=s["title"], venue=leg["venue"], ref=leg["market_id"],
            edge=round((fair - cost) / cost, 4) if cost and fair is not None else None, score=None,
            reason=f"{s['kind']} says {leg['side'].upper()} and the quote has not moved 3¢ yet · fair value is a rough guess",
            price_kind="market", price=_mid(by_q.get((leg["venue"], leg["market_id"]), {})), side=leg["side"])
    st["news_latency"] = {"n": len(lat), "note": "directional X burst or primary-source stance, last 60 min"
                          + ("" if lat else " · none unpriced right now")}

    # 3) X bursts on linked markets (evidence; tradeable only when directional)
    since = (now - timedelta(hours=2)).isoformat()
    xb = _q(conn, "SELECT entity, source, z, venue, market_id, title, price_then, price_now, status, direction, at "
                  "FROM x_bursts WHERE at>=? ORDER BY z DESC", (since,))
    seen_x = set()
    for ent, src, z, venue, mid, title, then, nowp, status, direction, at in xb:
        key = f"xb:{ent}:{venue}:{mid}"
        if key in seen_x:
            continue
        seen_x.add(key)
        add("x_burst", id=key, tier=1 if status == "not_priced_in" else 2,
            title=f"“{ent}” burst → {(title or mid or 'no linked market')[:90]}", venue=venue, ref=mid,
            edge=None, score=_f(z), source_at=_iso(at),
            reason=f"{src} burst z={_f(z) or 0:.1f} · {status.replace('_', ' ')}"
                   + (" · no stance, not tradeable alone" if not direction else ""),
            price_kind="market" if venue and mid else None,
            price=_mid(by_q.get((venue, mid), {})) if (venue, mid) in by_q else _f(nowp))
    st["x_burst"] = {"n": len(seen_x), "note": "X burst detector, last 2h" + ("" if seen_x else " · no bursts")}

    # 4) alpha screens: latest evaluation per token, passed only
    since = (now - timedelta(hours=2)).isoformat()
    al = _q(conn, "SELECT chain, address, symbol, evaluated_at, score, cap_class, price, liquidity, volume_24h, h1, h24, "
                  "screens_json FROM alpha_candidates WHERE passed=1 AND evaluated_at>=? ORDER BY evaluated_at DESC", (since,))
    for chain, addr, sym, at, score, cap, price, liq, vol, h1, h24, screens in al:
        try:
            sc = json.loads(screens or "{}")
        except ValueError:
            sc = {}
        gate = (sc.get("catalyst_gate") or {})
        why = "all rug screens pass" + ("; catalyst gate NOT met (no entry)" if gate and not gate.get("pass")
                                         else "; catalyst gate met")
        add("alpha", id=f"tok:{chain}:{addr}", tier=1 if gate.get("pass") else 2, title=f"{sym} ({cap or '?'} · {chain})", venue=chain, ref=addr,
            edge=None, score=_f(score), source_at=_iso(at),
            reason=f"{why} · liq ${(_f(liq) or 0):,.0f} · vol24h ${(_f(vol) or 0):,.0f} · 1h {(_f(h1) or 0):+.1f}%",
            price_kind="token", price=_f(price),
            url=f"https://dexscreener.com/{chain}/{addr}")
    st["alpha"] = {"n": len(al), "note": "tokens passing every rug screen, last 2h" + ("" if al else " · none passed")}

    # 5) culture radar: top movers by score, updated in the last hour
    since = (now - timedelta(hours=1)).isoformat()
    cu = _q(conn, "SELECT kind, entity, symbol, chain, title, domain, score, buzz_velocity, sources, updated_at, price, "
                  "price_change FROM culture_signals WHERE updated_at>=? AND kind IN ('token','ticker','market') "
                  "ORDER BY score DESC LIMIT 12", (since,))
    for kind, ent, sym, chain, title, dom, score, vel, srcs, at, price, chg in cu:
        add("culture", id=f"cul:{kind}:{ent}", tier=2, title=f"{(sym or '').upper() or ent}: {(title or '')[:80]}",
            venue=dom, ref=ent, edge=None, score=_f(score), source_at=_iso(at),
            reason=f"{kind} buzz score {(_f(score) or 0):.2f} · velocity {(_f(vel) or 0):.2f} · via {srcs}"
                   + (f" · Δ {(_f(chg) or 0):+.3f}" if chg is not None else ""),
            price_kind="culture", price=_f(price))
    st["culture"] = {"n": len(cu), "note": "top buzz by score, last 1h" + ("" if cu else " · no fresh signals")}

    # 6) trench sweeps: best-scored fresh pump.fun / DEX pools (watch only, every one is 'observe')
    since = (now - timedelta(minutes=20)).isoformat()
    tr = _q(conn, "SELECT candidate_id, token_mint, captured_at, reference_price_usd, reference_liquidity_usd, disposition, "
                  "opportunity_score, survival_risk FROM trench_candidates WHERE captured_at>=? AND disposition<>'quarantine' "
                  "ORDER BY opportunity_score DESC LIMIT 6", (since,))
    for cid, mint, at, px, liq, disp, score, risk in tr:
        add("trench", id=f"pool:{mint}", tier=2, title=f"pool {mint[:6]}…{mint[-4:]}", venue="solana", ref=mint,
            edge=None, score=_f(score), source_at=_iso(at),
            reason=f"{disp} · opportunity {(_f(score) or 0):.2f} · survival risk {(_f(risk) or 0):.2f} · liq ${(_f(liq) or 0):,.0f}",
            price_kind="trench", price=_f(px), url=f"https://dexscreener.com/solana/{mint}")
    st["trench"] = {"n": len(tr), "note": "best-scored fresh pools, last 20 min (watch only)"
                    + ("" if tr else " · no fresh pools")}

    # 7) strategy book: open / shadow signals
    since = (now - timedelta(hours=24)).isoformat()
    ss = _q(conn, "SELECT key, strategy, title, edge, status, at, reason, cost FROM strategy_signals "
                  "WHERE status IN ('open','shadow') AND at>=?", (since,))
    for key, strat, title, edge, status, at, reason, cost in ss:
        add("strategy", id=f"strat:{strat}:{key}", tier=1 if edge else 2, title=f"{strat}: {title}", venue=strat, ref=key,
            edge=_f(edge), score=None, source_at=_iso(at), reason=f"{status} · {reason or ''}".strip(" ·"),
            price_kind="cost", price=_f(cost))
    st["strategy"] = {"n": len(ss), "note": "open or shadow strategy signals, last 24h" + ("" if ss else " · none")}
    for k, v in st.items():
        v["status"] = "ok" if v["n"] else "empty"
        v["label"] = SOURCES[k]
    return items, st


def _current_price(conn: sqlite3.Connection, row: dict[str, Any]) -> float | None:
    """Latest known price for an item that is no longer in the live scan."""
    kind, ref, venue = row.get("price_kind"), row.get("ref"), row.get("venue")
    if kind == "market" and venue and ref:
        r = _q(conn, "SELECT snapshot_json FROM market_snapshots WHERE venue=? AND market_id=? ORDER BY captured_at DESC "
                     "LIMIT 1", (venue, ref))
        if r:
            try:
                return _mid(json.loads(r[0][0]))
            except ValueError:
                return None
    if kind == "token" and ref:
        r = _q(conn, "SELECT price FROM alpha_candidates WHERE address=? ORDER BY evaluated_at DESC LIMIT 1", (ref,))
        return _f(r[0][0]) if r else None
    if kind == "trench" and ref:
        r = _q(conn, "SELECT tick_json FROM trench_observations WHERE mint=? AND observed_at IS NOT NULL "
                     "ORDER BY observed_at DESC LIMIT 1", (ref,))
        if r:
            try:
                return _f(json.loads(r[0][0]).get("price_usd"))
            except ValueError:
                return None
    if kind == "culture" and ref:
        r = _q(conn, "SELECT price FROM culture_signals WHERE entity=? ORDER BY updated_at DESC LIMIT 1", (ref,))
        return _f(r[0][0]) if r else None
    return None


def _move(ref: float | None, last: float | None, kind: str | None) -> float | None:
    if ref is None or last is None:
        return None
    if kind in ("market", "culture") and ref <= 1 and last <= 1:
        return round(last - ref, 4)  # probability points
    return round(last / ref - 1, 4) if ref else None


def refresh(db_path: str, *, now: datetime | None = None, feed_path: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    iso = now.isoformat()
    fpath = feed_path or feed_db_path(db_path)
    if not Path(db_path).exists():
        return {"as_of": iso, "items": [], "history": [], "sources": {}, "error": "no trading database yet"}
    src = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    fc = _feed_conn(fpath)
    try:
        items, status = collect(src, now)
        live_ids = set()
        for it in items:
            if it["id"] in live_ids:
                continue
            live_ids.add(it["id"])
            row = fc.execute("SELECT first_seen, ref_price FROM opp_items WHERE id=?", (it["id"],)).fetchone()
            pj = json.dumps({k: it[k] for k in ("legs", "side") if k in it})
            if row is None:
                fc.execute("INSERT INTO opp_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (it["id"], it["source"], it["tier"], it["title"], it.get("venue"), it.get("ref"), iso, iso,
                            it.get("source_at"), it.get("price_kind"), it.get("price"), it.get("price"), it.get("edge"),
                            it.get("score"), it["reason"], it.get("url"), "live", None, pj))
            else:
                fc.execute("UPDATE opp_items SET tier=?, title=?, last_seen=?, source_at=?, last_price=COALESCE(?, last_price),"
                           " ref_price=COALESCE(ref_price, ?), edge=?, score=?, reason=?, status='live', expired_at=NULL,"
                           " payload_json=? WHERE id=?",
                           (it["tier"], it["title"], iso, it.get("source_at"), it.get("price"), it.get("price"),
                            it.get("edge"), it.get("score"), it["reason"], pj, it["id"]))
        # expire everything the sources no longer report; refresh their after-the-fact price
        for (oid, pk, ref, venue) in fc.execute("SELECT id, price_kind, ref, venue FROM opp_items WHERE status='live'").fetchall():
            if oid not in live_ids:
                p = _current_price(src, {"price_kind": pk, "ref": ref, "venue": venue})
                fc.execute("UPDATE opp_items SET status='expired', expired_at=?, last_price=COALESCE(?, last_price) "
                           "WHERE id=?", (iso, p, oid))
        cut = (now - timedelta(hours=HISTORY_H)).isoformat()
        for (oid, pk, ref, venue) in fc.execute("SELECT id, price_kind, ref, venue FROM opp_items WHERE status='expired' "
                                                "AND expired_at>=? ORDER BY expired_at DESC LIMIT 40", (cut,)).fetchall():
            p = _current_price(src, {"price_kind": pk, "ref": ref, "venue": venue})
            if p is not None:
                fc.execute("UPDATE opp_items SET last_price=? WHERE id=?", (p, oid))
        fc.execute("DELETE FROM opp_items WHERE status='expired' AND expired_at<?", (cut,))
        for k, v in status.items():
            fc.execute("INSERT INTO opp_scans VALUES (?,?,?,?)", (iso, k, v["n"], v["note"]))
        fc.execute("DELETE FROM opp_scans WHERE at<?", (cut,))
        fc.commit()
        return board(fc, status, now)
    finally:
        src.close()
        fc.close()


def _row(r: sqlite3.Row, now: datetime) -> dict[str, Any]:
    d = dict(r)
    d.pop("payload_json", None)
    fs = datetime.fromisoformat(d["first_seen"])
    d["age_s"] = round((now - fs).total_seconds())
    d["is_new"] = d["status"] == "live" and d["age_s"] <= NEW_FOR_S
    d["move_since_found"] = _move(d["ref_price"], d["last_price"], d["price_kind"])
    d["label"] = ("EDGE" if (d.get("edge") or 0) > 0 else "CANDIDATE") if d.get("tier", 2) <= 1 else "WATCH"
    d["move_unit"] = "pts" if d["price_kind"] in ("market", "culture") and (d["ref_price"] or 2) <= 1 else "pct"
    return d


def rank_key(d: dict[str, Any]) -> tuple:
    """Known positive edge after fees first, then candidates, then watch items; newest breaks ties."""
    e = d.get("edge")
    return (d.get("tier", 2), -(e if e is not None else -9), d.get("age_s") or 0, -(d.get("score") or 0))


def board(fc: sqlite3.Connection, status: dict[str, Any], now: datetime) -> dict[str, Any]:
    fc.row_factory = sqlite3.Row
    live = [_row(r, now) for r in fc.execute("SELECT * FROM opp_items WHERE status='live'")]
    live.sort(key=rank_key)
    hist = [_row(r, now) for r in fc.execute("SELECT * FROM opp_items WHERE status='expired' ORDER BY expired_at DESC LIMIT 30")]
    moved = [h["move_since_found"] for h in hist + live if h["move_since_found"] is not None and h["age_s"] >= 300]
    first = fc.execute("SELECT MIN(at) FROM opp_scans").fetchone()[0]
    fc.row_factory = None
    newest = max((d["first_seen"] for d in live), default=None)
    src_latest = max((d["source_at"] for d in live if d.get("source_at")), default=None)
    return {"as_of": now.isoformat(), "paper": True, "proven": False,
            "honesty": "Estimates only. No strategy here has a settled track record yet; edge after fees is shown only "
                       "where a cost model exists. Nothing in this feed places orders.",
            "items": live, "history": hist, "sources": status,
            "counts": {"live": len(live), "new": sum(1 for d in live if d["is_new"]), "history": len(hist),
                       "tracking_since": first},
            "newest_find_at": newest, "newest_source_row_at": src_latest,
            "measured": {"n": len(moved), "up": sum(1 for m in moved if m > 0), "down": sum(1 for m in moved if m < 0),
                         "note": "price move since first seen (≥5 min old), not P&L: no fees, slippage or fills"}}


def opportunity_feed(db_path: str, *, force: bool = False) -> dict[str, Any]:
    """Cached entry point for /api/opportunities (rescans at most every REFRESH_S seconds)."""
    with _LOCK:
        if not force and _LAST["payload"] is not None and _LAST["key"] == db_path and time.monotonic() - _LAST["at"] < REFRESH_S:
            return _LAST["payload"]
        p = refresh(db_path)
        _LAST.update(at=time.monotonic(), payload=p, key=db_path)
        return p
