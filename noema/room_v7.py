"""TEV room v7 overlay feed: real, read-only telemetry for the live trading room.

Everything here is derived from rows NOEMA already stores (desk events, crawl cycles,
news evidence, equity feed) or from public read-only price/Stripe GETs. Nothing is
synthesised for animation: when a source has no data the field is ``None`` and the UI
shows an explicit empty state.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sqlite3
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

STRIPE_KEY_ENV = "NOEMA_STRIPE_READ_KEY"
_CACHE: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()


def _cached(key: str, ttl: float, fn):
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _LOCK:
        _CACHE[key] = (now, value)
    return value


def _ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _j(text: Any) -> dict[str, Any]:
    try:
        v = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------- events / pulse
def _summarise(event: str, desk: str | None, p: dict[str, Any]) -> tuple[str, str]:
    sym = p.get("symbol") or ""
    if event == "chief_decision":
        verb = "APPROVED" if p.get("approved") else "VETOED"
        return ("approve" if p.get("approved") else "veto",
                f"CHIEF {verb} {sym} p={p.get('probability')} conf={p.get('confidence')} — {p.get('reason', '')}")
    if event == "desk_cycle":
        return ("cycle", (f"sweep: {p.get('pools_seen', 0)} pools → {p.get('prefilter_passed', 0)} filters → "
                          f"{p.get('holder_checked', 0)} holders → {p.get('entry_passed', 0)} judge → "
                          f"{p.get('entries', 0)} entered"))
    if event == "paper_entry":
        return ("entry", f"{desk} PAPER BUY {sym} ${round(float(p.get('allocation_usd') or 0), 2)} — {p.get('reason', '')}")
    if event == "desk_hired":
        return ("hire", f"HIRED {p.get('name') or desk} bankroll ${round(float(p.get('bankroll_usd') or 0), 2)}")
    if event == "desk_fired":
        return ("fire", f"FIRED {desk} — {p.get('reason', '')}")
    if event == "equity_snapshot":
        return ("equity", f"paper equity ${p.get('equity_usd')} (pnl {p.get('pnl_after_costs_usd')})")
    return ("other", f"{event.upper()} {desk or ''} {sym}".strip())


def pulse(conn: sqlite3.Connection, since_id: int = 0, limit: int = 60) -> list[dict[str, Any]]:
    rows = _rows(conn, "SELECT id, ts, desk_id, event, payload_json FROM trench_desk_events "
                       "WHERE id>? AND event!='equity_snapshot' ORDER BY id DESC LIMIT ?",
                 (int(since_id), int(limit)))
    out = []
    for rid, ts, desk, event, payload in rows:
        kind, text = _summarise(event, desk, _j(payload))
        out.append({"id": rid, "ts": ts, "desk": desk, "event": event, "kind": kind, "text": text})
    return out


# ---------------------------------------------------------------- funnel + scan lane
STAGES = ("crawled", "filters", "holders", "judged", "entered")


def _closes(candles: list) -> list[float]:
    out = []
    for c in candles or []:
        if isinstance(c, (list, tuple)) and len(c) >= 5:
            try:
                out.append(float(c[4]))
            except (TypeError, ValueError):
                continue
    return out[-48:]


def _ohlc(candles: list, n: int = 48) -> list[list[float]]:
    """Real provider OHLCV rows [t, o, h, l, c, v] (GeckoTerminal), newest n, never synthesised."""
    out = []
    for c in candles or []:
        if isinstance(c, (list, tuple)) and len(c) >= 5:
            try:
                row = [float(v) for v in c[:6]]
            except (TypeError, ValueError):
                continue
            out.append(row + [0.0] * (6 - len(row)))
    out.sort(key=lambda r: r[0])
    return out[-n:]


def _interval_label(rows: list) -> str | None:
    if len(rows) < 2:
        return None
    d = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]))
    s = d[len(d) // 2]
    return f"{round(s / 3600)}h" if s >= 3600 else f"{max(1, round(s / 60))}m"


PM_BUCKETS = (900, 3600, 4 * 3600, 24 * 3600)


def pm_candles(conn: sqlite3.Connection, *, markets: int = 6, min_quotes: int = 4,
               lookback_hours: int = 72) -> dict[str, Any]:
    """Aggregate our stored Kalshi/Polymarket quote snapshots (yes mid) into OHLC candles.

    Volume is the number of stored quotes in the bucket (we do not store traded volume).
    The interval is auto-picked per market (15m/1h/4h/1d) so each chart has ~24 candles."""
    since = (datetime.now(UTC) - timedelta(hours=lookback_hours)).isoformat()
    try:
        tops = conn.execute(
            "SELECT venue, market_id, COUNT(*) n FROM market_snapshots WHERE captured_at >= ? AND valid = 1 "
            "GROUP BY venue, market_id HAVING n >= ? ORDER BY n DESC LIMIT ?",
            (since, min_quotes, markets)).fetchall()
        total = conn.execute("SELECT COUNT(DISTINCT market_id), COUNT(*) FROM market_snapshots WHERE captured_at >= ?",
                             (since,)).fetchone()
    except sqlite3.Error:
        return {"markets": [], "status": "no market_snapshots table"}
    out = []
    for venue, mid, _n in tops:
        rows = conn.execute("SELECT captured_at, snapshot_json FROM market_snapshots WHERE venue=? AND market_id=? "
                            "AND captured_at >= ? ORDER BY captured_at", (venue, mid, since)).fetchall()
        pts, title = [], mid
        for at, js in rows:
            d = _j(js)
            title = d.get("title") or title
            b, a = d.get("yes_bid"), d.get("yes_ask")
            try:
                px = (float(b) + float(a)) / 2 if b is not None and a is not None else float(b if b is not None else a)
                t = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                continue
            pts.append((t, px))
        if len(pts) < min_quotes:
            continue
        span = pts[-1][0] - pts[0][0]
        bucket = next((b for b in PM_BUCKETS if span / b <= 36), PM_BUCKETS[-1])
        candles: dict[int, list[float]] = {}
        for t, px in pts:
            k = int(t // bucket * bucket)
            c = candles.get(k)
            if c is None:
                candles[k] = [float(k), px, px, px, px, 1.0]
            else:
                c[2], c[3], c[4], c[5] = max(c[2], px), min(c[3], px), px, c[5] + 1
        marks = []
        try:
            for (qa,) in conn.execute("SELECT quoted_at FROM paper_quotes WHERE market_id=? AND selected=1", (mid,)):
                marks.append({"t": datetime.fromisoformat(qa).timestamp(), "kind": "entry"})
        except (sqlite3.Error, ValueError, TypeError):
            pass
        lab = f"{bucket // 3600}h" if bucket >= 3600 else f"{bucket // 60}m"
        out.append({"venue": venue, "market_id": mid, "title": title, "interval": lab,
                    "ohlc": sorted(candles.values())[-48:], "quotes": len(pts), "markers": marks})
    return {"markets": out, "markets_seen": total[0] if total else 0, "quotes_seen": total[1] if total else 0,
            "min_quotes": min_quotes, "lookback_hours": lookback_hours,
            "status": "ok" if out else f"no market has {min_quotes}+ stored quotes in {lookback_hours}h"}


def _scan_chart(it: dict, sweep_ts: Any) -> dict[str, Any]:
    rows = _ohlc(it.get("candles"))
    marks = []
    st, why = str(it.get("stage") or ""), str(it.get("reason") or "")
    try:
        t = datetime.fromisoformat(str(sweep_ts).replace("Z", "+00:00")).timestamp() if sweep_ts else None
    except ValueError:
        t = None
    if rows and t is not None:
        if re.search(r"enter|entry|approved|opened", st + " " + why, re.I) and st != "rejected":
            marks.append({"t": t, "kind": "entry"})
        elif st != "rejected":
            marks.append({"t": t, "kind": "find"})
    return {"ohlc": rows, "interval": _interval_label(rows), "markers": marks}


def funnel(conn: sqlite3.Connection, cycles: int = 30) -> dict[str, Any]:
    rows = _rows(conn, "SELECT id, ts, payload_json FROM trench_desk_events WHERE event='desk_cycle' "
                       "ORDER BY id DESC LIMIT ?", (cycles,))
    series = []
    latest: dict[str, Any] = {}
    for i, (rid, ts, payload) in enumerate(rows):
        p = _j(payload)
        if i == 0:
            latest = p | {"_id": rid, "_ts": ts}
        series.append({"id": rid, "ts": ts, "crawled": int(p.get("pools_seen") or 0),
                       "filters": int(p.get("prefilter_passed") or 0),
                       "holders": int(p.get("holder_checked") or 0),
                       "judged": int(p.get("entry_passed") or 0), "entered": int(p.get("entries") or 0),
                       "pumpfun": int(p.get("pumpfun_new_launches") or 0)})
    series.reverse()
    rej = latest.get("rejections") or {}
    rejects = sorted(({"reason": k, "count": int(v)} for k, v in rej.items() if isinstance(v, (int, float))),
                     key=lambda r: -r["count"])
    scan = []
    for it in (latest.get("feed") or [])[:40]:
        if not isinstance(it, dict):
            continue
        scan.append({k: it.get(k) for k in ("symbol", "mint", "source", "stage", "reason", "price_usd",
                                            "liquidity_usd", "volume_h1_usd", "price_change_m5",
                                            "pool_age_hours", "trades_h1", "fdv_usd",
                                            "top10_holder_fraction", "probability", "confidence")}
                    | {"closes": _closes(it.get("candles"))} | _scan_chart(it, latest.get("_ts")))
    return {"cycle_id": latest.get("_id"), "ts": latest.get("_ts"), "series": series,
            "latest": series[-1] if series else None, "rejects": rejects, "scan": scan,
            "judgement": latest.get("judgement"), "chief_gate": latest.get("chief"),
            "pumpfun_new": latest.get("pumpfun_new_launches")}


def chief_state(conn: sqlite3.Connection, fun: dict[str, Any]) -> dict[str, Any]:
    row = _rows(conn, "SELECT ts, payload_json FROM trench_desk_events WHERE event='chief_decision' "
                      "ORDER BY id DESC LIMIT 1")
    last = (_j(row[0][1]) | {"ts": row[0][0]}) if row else None
    gate = fun.get("chief_gate") or {}
    lt = fun.get("latest") or {}
    if lt and not lt.get("judged"):
        waiting = (f"waiting on a candidate: last sweep {lt.get('crawled', 0)} pools, "
                   f"{lt.get('filters', 0)} passed filters, {lt.get('holders', 0)} holder-checked, "
                   "none reached the judge")
    elif lt:
        waiting = f"{lt.get('judged')} candidate(s) with the judge; gate p≥{gate.get('min_probability', 0.6)} conf≥{gate.get('min_confidence', 0.55)}"
    else:
        waiting = "waiting on the first crawler sweep"
    return {"last": last, "waiting_on": waiting, "gate": gate}


# ---------------------------------------------------------------- equity / news / regime
def equity_curve(conn: sqlite3.Connection) -> dict[str, Any]:
    real = [{"at": at, "total_usd": float(t)} for at, t in
            _rows(conn, "SELECT at, total_usd FROM equity_feed_readings WHERE total_usd IS NOT NULL "
                        "ORDER BY at DESC LIMIT 300")][::-1]
    guard = _rows(conn, "SELECT equity, peak_equity, floor, day_start_equity FROM capital_guard "
                        "ORDER BY id DESC LIMIT 1")
    paper = [{"at": ts, "equity_usd": _j(p).get("equity_usd")} for ts, p in
             _rows(conn, "SELECT ts, payload_json FROM trench_desk_events WHERE event='equity_snapshot' "
                         "ORDER BY id DESC LIMIT 300")][::-1]
    g = None
    if guard:
        eq, peak, floor, day0 = guard[0]
        g = {"equity_usd": float(eq), "peak_usd": float(peak), "floor_usd": float(floor),
             "day_pnl_usd": float(eq) - float(day0)}
    return {"real": real, "paper": paper, "guard": g}


def news_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    since = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    n = _rows(conn, "SELECT COUNT(*) FROM news_evidence WHERE retrieved_at>=?", (since,))
    sched = _rows(conn, "SELECT name, last_status, last_fetch_at, failures, last_new FROM news_feed_schedule")
    leds = [{"name": a, "status": str(b) if b is not None else None, "last_fetch_at": c,
             "failures": d or 0, "last_new": e} for a, b, c, d, e in sched]
    return {"articles_30m": n[0][0] if n else 0,
            "articles_per_min": round((n[0][0] if n else 0) / 30, 2), "sources": leds}


def regime(fun: dict[str, Any], prices: dict[str, Any]) -> dict[str, Any]:
    moves = [s["price_change_m5"] for s in fun.get("scan", []) if isinstance(s.get("price_change_m5"), (int, float))]
    if not moves:
        return {"label": "UNKNOWN", "basis": "no scanned pools yet"}
    breadth = sum(1 for m in moves if m > 0) / len(moves)
    med = statistics.median(moves)
    spread = statistics.pstdev(moves) if len(moves) > 1 else 0.0
    btc = (prices.get("BTC") or {}).get("change_24h_pct")
    if spread > 8:
        label = "VOLATILE"
    elif breadth >= 0.6 and med > 0:
        label = "RISK-ON"
    elif breadth <= 0.4 and med < 0:
        label = "RISK-OFF"
    else:
        label = "CHOP"
    return {"label": label, "breadth": round(breadth, 3), "median_m5_pct": round(med, 3),
            "dispersion": round(spread, 3), "btc_24h_pct": btc, "n": len(moves),
            "basis": f"{len(moves)} scanned Solana pools (5m moves) from the last sweep"}


# ---------------------------------------------------------------- external read-only
def _fetch_prices() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        r = httpx.get("https://api.coinbase.com/v2/exchange-rates", params={"currency": "USD"}, timeout=4)
        rates = r.json()["data"]["rates"]
        for sym in ("BTC", "ETH", "SOL"):
            out[sym] = {"usd": round(1 / float(rates[sym]), 4), "source": "coinbase"}
    except Exception:  # noqa: BLE001 - feed is optional; UI shows "—"
        return {}
    try:
        r = httpx.get("https://api.coingecko.com/api/v3/simple/price", timeout=4,
                      params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd",
                              "include_24hr_change": "true"})
        d = r.json()
        for sym, cid in (("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana")):
            ch = (d.get(cid) or {}).get("usd_24h_change")
            if ch is not None and sym in out:
                out[sym]["change_24h_pct"] = round(float(ch), 2)
    except Exception:  # noqa: BLE001, S110 - 24h change is optional decoration of a real spot price
        pass
    out["_at"] = datetime.now(UTC).isoformat()
    return out


def prices() -> dict[str, Any]:
    return _cached("prices", 20, _fetch_prices)


def _stripe_get(key: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = httpx.get(f"https://api.stripe.com/v1/{path}", params=params or {}, timeout=6,
                  auth=(key, ""), headers={"Stripe-Version": "2024-06-20"})
    r.raise_for_status()
    return r.json()


def _amounts(rows: list) -> list[dict[str, Any]]:
    return [{"currency": r.get("currency"), "amount_minor": r.get("amount")} for r in rows or []
            if isinstance(r, dict) and isinstance(r.get("amount"), int)]


def _fetch_stripe(key: str) -> dict[str, Any]:
    try:
        bal = _stripe_get(key, "balance")
        ch = _stripe_get(key, "charges", {"limit": 8})
        po = _stripe_get(key, "payouts", {"limit": 5})
    except httpx.HTTPStatusError as exc:
        return {"status": "error", "reason": f"Stripe returned HTTP {exc.response.status_code}"}
    except Exception:  # noqa: BLE001
        return {"status": "error", "reason": "Stripe unreachable"}
    return {
        "status": "connected", "livemode": bal.get("livemode"),
        "available": _amounts(bal.get("available")), "pending": _amounts(bal.get("pending")),
        "payments": [{"created": c.get("created"), "amount_minor": c.get("amount"),
                      "currency": c.get("currency"), "status": c.get("status"),
                      "paid": c.get("paid"), "refunded": c.get("refunded")} for c in ch.get("data", [])],
        "payouts": [{"created": p.get("created"), "arrival_date": p.get("arrival_date"),
                     "amount_minor": p.get("amount"), "currency": p.get("currency"),
                     "status": p.get("status")} for p in po.get("data", [])],
        "at": datetime.now(UTC).isoformat(),
    }


def stripe_panel(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Finance view only. Never used to fund trades. Key never leaves this process."""
    env = os.environ if env is None else env
    key = env.get(STRIPE_KEY_ENV, "").strip()
    if not key:
        return {"status": "not_connected", "env": STRIPE_KEY_ENV,
                "reason": f"set {STRIPE_KEY_ENV} to a restricted read-only Stripe key (rk_…)"}
    if not key.startswith("rk_"):
        return {"status": "refused", "env": STRIPE_KEY_ENV,
                "reason": "only restricted read-only keys (rk_…) are accepted for the finance panel"}
    return _cached("stripe", 60, lambda: _fetch_stripe(key))


# ---------------------------------------------------------------- headline
def headline(chief: dict[str, Any], fun: dict[str, Any], events: list[dict[str, Any]],
             reg: dict[str, Any]) -> str:
    last = chief.get("last")
    lt = fun.get("latest") or {}
    top = (fun.get("rejects") or [{}])[0]
    parts = []
    if lt:
        parts.append(f"Scanned {lt['crawled']} pools; {lt['filters']} survived filters"
                     + (f", most cut for '{top['reason']}' ({top['count']})" if top.get("reason") else ""))
    if last:
        parts.append(f"last call: {'APPROVE' if last.get('approved') else 'VETO'} {last.get('symbol')} "
                     f"({last.get('reason')})")
    else:
        parts.append(chief.get("waiting_on", ""))
    if reg.get("label") not in (None, "UNKNOWN"):
        parts.append(f"tape reads {reg['label']}")
    return " · ".join(p for p in parts if p) or "Waiting on the first real crawler sweep."


# ---------------------------------------------------------------- crawler hero feed
def _iso(v: Any) -> str | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).isoformat()


def _host(url: str) -> str:
    u = str(url or "")
    return u.split("://", 1)[-1].split("/", 1)[0]


LIGHTS = ("news", "x", "culture", "dexscreener", "geckoterminal", "pumpfun", "kalshi", "polymarket", "rpc")


def light_of(h: dict[str, Any]) -> str:
    """Which source light a recorded fetch belongs to (venue/provider names win over the kind)."""
    s = f"{h.get('source') or ''} {h.get('url') or ''}".lower()
    kind = h.get("kind")
    if kind == "x":
        return "x"
    if kind == "pumpfun" or "pump.fun" in s or "pumpfun" in s:
        return "pumpfun"
    if "dexscreener" in s:
        return "dexscreener"
    if "geckoterminal" in s:
        return "geckoterminal"
    if kind == "prediction" and "kalshi" in s:
        return "kalshi"
    if kind == "prediction" and "polymarket" in s:
        return "polymarket"
    return str(kind)


def _latest_by_light(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Newest recorded fetch per source light over ALL hits, not just the truncated feed list."""
    out: dict[str, Any] = {}
    for h in hits:  # already newest first
        k = light_of(h)
        if k in LIGHTS and k not in out:
            out[k] = {"at": h["at"], "source": h["source"], "url": h["url"], "status": h["status"]}
    return out


def crawl_feed(conn: sqlite3.Connection, fun: dict[str, Any] | None = None, limit: int = 60) -> dict[str, Any]:
    """Every real fetch the crawlers recorded, newest first, plus counters and next-due times.

    Sources: news_fetch_state (news GETs), culture_source_state (DexScreener, pump.fun, Reddit,
    StockTwits, Kalshi/Polymarket culture), x_ingest_runs (X reads), market_scan_state (Kalshi and
    Polymarket market sweeps), trench_desk_events desk_cycle (trench pool sweeps) and
    trench_provider_health. Nothing is invented: an empty table yields no rows.
    """
    fun = fun or {}
    hits: list[dict[str, Any]] = []
    for url, status, at in _rows(conn, "SELECT url, status, fetched_at FROM news_fetch_state "
                                      "ORDER BY fetched_at DESC LIMIT 40"):
        hits.append({"at": _iso(at), "kind": "news", "source": _host(url), "url": url,
                     "status": str(status) if status is not None else None, "items": None})
    for name, dom, url, status, items, at in _rows(
            conn, "SELECT name, domain, url, status, items, fetched_at FROM culture_source_state"):
        kind = "pumpfun" if "pump" in str(name).lower() else (
            "prediction" if dom == "prediction" else "culture")
        hits.append({"at": _iso(at), "kind": kind, "source": name, "url": url,
                     "status": str(status) if status is not None else None, "items": items})
    for at, seen, new, linked in _rows(conn, "SELECT at, posts_seen, posts_new, linked FROM x_ingest_runs "
                                            "ORDER BY id DESC LIMIT 5"):
        hits.append({"at": _iso(at), "kind": "x", "source": "X read (political · crypto · stocks)",
                     "url": "x-inbox: Grok Bot X read tools", "status": "ok", "items": seen,
                     "detail": f"{new} new · {linked} linked to markets"})
    for src, at in _rows(conn, "SELECT source, updated_at FROM market_scan_state"):
        venue = str(src).split(":")[0]
        url = ("https://api.elections.kalshi.com/trade-api/v2/markets" if venue.startswith("kalshi")
               else "https://api.polymarket.us/v1/markets" if "polymarket" in venue else str(src))
        hits.append({"at": _iso(at), "kind": "prediction", "source": src, "url": url,
                     "status": "ok", "items": None})
    for prov, state, at, err in _rows(conn, "SELECT provider, state, last_attempt_at, last_error_class "
                                           "FROM trench_provider_health"):
        hits.append({"at": _iso(at), "kind": "rpc", "source": prov, "url": prov,
                     "status": state if state == "healthy" else f"{state}: {err or '?'}", "items": None})
    for it in (fun.get("scan") or [])[:12]:
        src = str(it.get("source") or "pool")
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{it.get('mint')}" \
            if "gecko" in src else f"https://api.dexscreener.com/latest/dex/tokens/{it.get('mint')}"
        hits.append({"at": _iso(fun.get("ts")), "kind": "pool", "source": f"{src} · {it.get('symbol')}",
                     "url": url, "status": str(it.get("stage") or ""), "items": None})
    hits = [h for h in hits if h["at"]]
    hits.sort(key=lambda h: h["at"], reverse=True)
    now = datetime.now(UTC)
    hour = (now - timedelta(hours=1)).isoformat()
    day = (now - timedelta(hours=24)).isoformat()
    sweeps_24h = _rows(conn, "SELECT COUNT(*) FROM trench_desk_events WHERE event='desk_cycle' AND ts>=?", (day,))
    pools_24h = _rows(conn, "SELECT payload_json FROM trench_desk_events WHERE event='desk_cycle' AND ts>=?", (day,))
    ser = fun.get("series") or []
    gaps = []
    for a, b in itertools.pairwise(ser):
        try:
            gaps.append((datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds())
        except (TypeError, ValueError, KeyError):
            continue
    nxt: dict[str, Any] = {}
    if fun.get("ts") and gaps:
        nxt["trench"] = (datetime.fromisoformat(fun["ts"]) + timedelta(seconds=statistics.median(gaps))).isoformat()
    due = _rows(conn, "SELECT MIN(next_due) FROM news_feed_schedule")
    if due and due[0][0]:
        nxt["news"] = _iso(due[0][0])
    xl = _rows(conn, "SELECT MAX(at) FROM x_ingest_runs")
    if xl and xl[0][0]:
        nxt["x"] = (datetime.fromisoformat(_iso(xl[0][0])) + timedelta(minutes=30)).isoformat()
    return {"hits": hits[:limit], "latest": _latest_by_light(hits),
            "counters": {"fetches_1h": sum(1 for h in hits if h["at"] >= hour),
                         "sources": len({h["source"] for h in hits if h["kind"] != "pool"}),
                         "sweeps_24h": sweeps_24h[0][0] if sweeps_24h else 0,
                         "pools_24h": sum(int(_j(p).get("pools_seen") or 0) for (p,) in pools_24h),
                         "sweep_interval_s": round(statistics.median(gaps)) if gaps else None},
            "next": nxt}


def build_room_v7(db_path: str, *, since_id: int = 0, fetch: bool = True,
                  env: dict[str, str] | None = None) -> dict[str, Any]:
    t0 = time.time()
    px = prices() if fetch else {}
    out: dict[str, Any] = {"as_of": datetime.now(UTC).isoformat(), "paper": True, "prices": px,
                           "events": [], "funnel": {"series": [], "rejects": [], "scan": []},
                           "chief": {"last": None, "waiting_on": "no database yet"},
                           "equity": {"real": [], "paper": [], "guard": None},
                           "news": {"articles_per_min": None, "sources": []},
                           "crawl": {"hits": [], "counters": {}, "next": {}},
                           "pm_candles": {"markets": [], "status": "no database yet"},
                           "stripe": stripe_panel(env) if fetch else {"status": "not_fetched"}}
    if Path(db_path).exists():
        conn = _ro(db_path)
        try:
            out["events"] = pulse(conn, since_id)
            out["funnel"] = funnel(conn)
            out["chief"] = chief_state(conn, out["funnel"])
            out["equity"] = equity_curve(conn)
            out["news"] = news_rate(conn)
            out["crawl"] = crawl_feed(conn, out["funnel"])
            out["pm_candles"] = pm_candles(conn)
        finally:
            conn.close()
    out["regime"] = regime(out["funnel"], px)
    out["headline"] = headline(out["chief"], out["funnel"], out["events"], out["regime"])
    out["generated_ms"] = round((time.time() - t0) * 1000)
    return out
