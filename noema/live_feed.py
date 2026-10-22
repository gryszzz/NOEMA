"""Live TEV snapshot for server push (SSE) - READ ONLY, paper only, never reads or emits secrets.

One builder (`build_live`) feeds both /api/live (poll fallback) and /api/live/stream (SSE). Every section carries
`as_of` (age of its newest underlying row) and `cadence_s` (how often that data is expected to move) so the UI can
show green / amber / red freshness honestly.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

_CACHE: dict[str, tuple[float, Any]] = {}
TFS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


def _cached(key: str, ttl: float, fn: Any) -> Any:
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as exc:  # noqa: BLE001 - a panel degrades to an explicit error, never breaks the stream
        err = f"{type(exc).__name__}: {str(exc)[:120]}"
        # keep the last good panel (its as_of shows the staleness) and retry in ~5 s, never cache an error for long
        val = {**hit[1], "last_error": err} if hit and "error" not in hit[1] else {"error": err, "as_of": None}
        _CACHE[key] = (time.monotonic() - max(0.0, ttl - 5), val)
        return val
    _CACHE[key] = (time.monotonic(), val)
    return val


def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    c.execute("PRAGMA query_only=1")
    return c


def _iso(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), UTC).isoformat()
    return str(v).replace(" ", "T") if "T" in str(v) or " " in str(v) else str(v)


def _ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace(" ", "T")).timestamp()


def _ci(xs: list[float], n: int = 400) -> list[float] | None:
    if len(xs) < 5:
        return None
    xs = xs[-1500:]
    rng = random.Random(11)
    k = len(xs)
    m = sorted(sum(rng.choice(xs) for _ in range(k)) / k for _ in range(n))
    return [round(m[int(0.025 * n)], 4), round(m[int(0.975 * n) - 1], 4)]


def _j(s: Any) -> Any:
    try:
        return json.loads(s) if s else {}
    except (TypeError, ValueError):
        return {}


# ---- launch stream -------------------------------------------------------------
def launch_section(db: str, now: float) -> dict[str, Any]:
    c = _ro(db)
    try:
        cr = c.execute("SELECT mint, name, symbol, received_at, creator, market_cap_sol FROM launch_stream "
                       "ORDER BY received_at DESC LIMIT 14").fetchall()
        tr = c.execute("SELECT signature, mint, received_at, side, sol, trader, market_cap_sol, source FROM launch_trades "
                       "ORDER BY rowid DESC LIMIT 6000").fetchall()
        seen = c.execute("SELECT source, received_at FROM launch_trade_seen ORDER BY rowid DESC LIMIT 20000").fetchall()
        ll = c.execute("SELECT MAX(received_at), COUNT(*) FROM launch_trades WHERE source='helius_launchlab'").fetchone()
    finally:
        c.close()
    sym = {m: (s or n or m[:6]) for m, n, s, *_ in cr}
    per: dict[str, list[float]] = defaultdict(list)
    cnt: Counter[str] = Counter()
    for _, mint, at, *_rest in tr:
        cnt[mint] += 1
    for src, at in seen:  # every source's own arrival (deduped trades still count for that source)
        per[src or "pumpportal"].append(_ts(at))
    if not seen:
        for r in tr:
            per[r[7] or "pumpportal"].append(_ts(r[2]))
    health = {}
    for src, ts in per.items():
        last = max(ts)
        n5 = sum(1 for t in ts if t >= now - 300)
        health[src] = {"last_at": _iso(last), "per_min": round(n5 / 5, 1), "status": "ok" if now - last < 60 else
                       "stale" if now - last < 600 else "down"}
    if "helius_launchlab" not in health and ll and ll[0]:
        health["helius_launchlab"] = {"last_at": _iso(ll[0]), "per_min": 0.0, "status": "idle (few LaunchLab trades)",
                                      "total": ll[1]}
    for src in ("helius", "pumpportal", "helius_launchlab"):
        health.setdefault(src, {"last_at": None, "per_min": 0.0, "status": "down (no trades in recent window)"})
    note = _pumpportal_note()
    if note and health["pumpportal"]["status"] != "ok":
        health["pumpportal"]["status"] = note
    if cr:
        health["creates"] = {"last_at": _iso(cr[0][3]), "status": "ok" if now - _ts(cr[0][3]) < 120 else "stale"}
    ticker = [{"at": _iso(at), "mint": m, "sym": sym.get(m, m[:6]), "side": side, "sol": round(sol or 0, 4),
               "trader": (tr_ or "")[:4] + "…" + (tr_ or "")[-4:] if tr_ else None, "mcap": mc, "src": src or "pumpportal"}
              for _, m, at, side, sol, tr_, mc, src in tr[:30]]
    return {"as_of": _iso(tr[0][2]) if tr else None, "cadence_s": 5,
            "creates": [{"mint": m, "name": n, "sym": s, "at": _iso(at), "dev": (cre or "")[:4] + "…" if cre else None,
                         "mcap": mc, "trades_recent": cnt.get(m, 0)} for m, n, s, at, cre, mc in cr],
            "ticker": ticker, "health": health}


def _pumpportal_note() -> str | None:
    """Latest PumpPortal server message from the stream log (the log never contains the key)."""
    p = os.getenv("NOEMA_LAUNCH_LOG", "/workspace/noema-run/launch_stream.log")
    try:
        with open(p, "rb") as f:
            f.seek(max(0, os.path.getsize(p) - 200_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for ln in reversed(tail.splitlines()):
        if "pumpportal msg" in ln:
            if "funded with at least" in ln:
                return "rejected: PumpPortal says API-key wallet is not funded (needs ≥0.02 SOL)"
            return None
    return None


def _bucket(span: float) -> int:
    want = max(1.0, span / 40)
    return next((t for t in TFS if t >= want), TFS[-1])


def candles_section(db: str, now: float, k: int = 6) -> dict[str, Any]:
    c = _ro(db)
    out = []
    try:
        fresh = c.execute("SELECT mint, name, symbol, received_at FROM launch_stream ORDER BY received_at DESC LIMIT 80"
                          ).fetchall()
        for mint, name, sym, found in fresh:
            rows = c.execute("SELECT received_at, market_cap_sol, sol FROM launch_trades WHERE mint=? AND market_cap_sol>0 "
                             "ORDER BY received_at", (mint,)).fetchall()
            if len(rows) < 12:
                continue
            pts = [(_ts(a), float(p), float(v or 0)) for a, p, v in rows]
            t0, t1 = pts[0][0], pts[-1][0]
            tf = _bucket(t1 - t0)
            bars: dict[int, list[float]] = {}
            for t, p, v in pts:
                b = int(t // tf * tf)
                if b not in bars:
                    bars[b] = [b, p, p, p, p, v]
                else:
                    x = bars[b]
                    x[2], x[3], x[4], x[5] = max(x[2], p), min(x[3], p), p, x[5] + v
            out.append({"mint": mint, "name": name, "sym": sym or (name or mint[:6]), "found_at": _iso(found),
                        "tf_s": tf, "n_trades": len(pts), "last_at": _iso(t1), "unit": "mcap SOL",
                        "rows": [[r[0], round(r[1], 4), round(r[2], 4), round(r[3], 4), round(r[4], 4), round(r[5], 3)]
                                 for r in sorted(bars.values())]})
            if len(out) >= k:
                break
    finally:
        c.close()
    return {"as_of": max((x["last_at"] for x in out), default=None), "cadence_s": 10, "charts": out}


# ---- smart wallets ---------------------------------------------------------------
def smart_section(smart_db: str) -> dict[str, Any]:
    from .smart_wallets import latest_report
    r = latest_report(smart_db) or {}
    sf = r.get("smart_follow") or {}
    return {"as_of": r.get("at"), "cadence_s": 900,
            "vetted": [{k: v.get(k) for k in ("wallet", "n_closed", "win_rate", "pnl_sol", "pnl_ex_best", "ci_lo")}
                       for v in (r.get("vetted") or [])[:12]],
            "n_vetted": len(r.get("vetted") or []), "candidates": r.get("candidates"),
            "moves": (r.get("recent_moves") or [])[:20], "rejections": r.get("rejections"),
            "smart_follow": {k: v for k, v in sf.items() if k != "recent"}, "moves_as_of":
                max((m["at"] for m in r.get("recent_moves") or []), default=None)}


# ---- paper agents (all rosters) -----------------------------------------------------
def agents_section(dd: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    newest: list[str] = []
    av = os.path.join(dd, "edge_avenues.db")
    by: dict[str, list[tuple[str, float]]] = defaultdict(list)
    if os.path.isfile(av):
        c = _ro(av)
        try:
            for kind, at, net in c.execute("SELECT kind, at, net FROM paper_outcomes ORDER BY at"):
                by[kind].append((at, float(net or 0)))
        finally:
            c.close()
    lc = os.path.join(dd, "agent_lifecycle.db")
    seen_kinds = set()
    if os.path.isfile(lc):
        c = _ro(lc)
        try:
            for name, kind, status, stj, hired in c.execute(
                    "SELECT name, kind, status, stats_json, hired_at FROM agents WHERE status!='fired' ORDER BY kind, name"):
                st = _j(stj)
                xs = [n for _, n in by.get(kind, [])]
                seen_kinds.add(kind)
                n = st.get("n") or len(xs)
                rows.append({"roster": "lifecycle", "name": name, "kind": kind, "status": status, "n": n,
                             "pnl": round(sum(xs), 4) if xs else st.get("mean_net"),
                             "mean": round(sum(xs) / len(xs), 4) if xs else st.get("mean_net"),
                             "ci": _ci(xs) if xs else st.get("ci95"), "since": hired})
                if by.get(kind):
                    newest.append(by[kind][-1][0])
        finally:
            c.close()
    for kind, v in by.items():
        if kind not in seen_kinds:
            xs = [n for _, n in v]
            rows.append({"roster": "avenue", "name": kind, "kind": kind, "status": "paper (not on roster yet)", "n": len(xs),
                         "pnl": round(sum(xs), 4), "mean": round(sum(xs) / len(xs), 4), "ci": _ci(xs)})
            newest.append(v[-1][0])
    pm = os.path.join(dd, "pm_train.db")
    if os.path.isfile(pm):
        c = _ro(pm)
        try:
            for aid, status, stj in c.execute("SELECT agent_id, status, stats_json FROM pm_agents WHERE status!='fired'"):
                st = _j(stj)
                rows.append({"roster": "pm", "name": aid, "kind": aid.split("-")[1] if "-" in aid else "pm",
                             "status": status, "n": st.get("n"), "pnl": st.get("paper_pnl", st.get("paper_net")),
                             "mean": st.get("brier_minus_market"), "ci": st.get("brier_diff_ci95"),
                             "metric": "Brier − market (lower is better)"})
        finally:
            c.close()
    tm = os.path.join(dd, "timing.db")
    if os.path.isfile(tm):
        c = _ro(tm)
        try:
            res: dict[str, list[float]] = defaultdict(list)
            for aid, net in c.execute("SELECT agent_id, net FROM t_results"):
                res[aid].append(float(net or 0))
            for aid, name, status in c.execute("SELECT agent_id, name, status FROM t_agents WHERE status!='fired'"):
                xs = res.get(aid, [])
                rows.append({"roster": "timing", "name": name or aid, "kind": "timing", "status": status, "n": len(xs),
                             "pnl": round(sum(xs), 4), "mean": round(sum(xs) / len(xs), 4) if xs else None, "ci": _ci(xs)})
            r = c.execute("SELECT MAX(scored_at) FROM t_results").fetchone()
            if r and r[0]:
                newest.append(_iso(r[0]))
        finally:
            c.close()
    return {"as_of": max((x for x in newest if x), default=None), "cadence_s": 600, "agents": rows,
            "note": "paper only; hire/fire paused (agent_runtime not started)"}


# ---- what each agent is looking at -------------------------------------------------
def looking_section(dd: str, main_db: str, smart_db: str, now: float) -> dict[str, Any]:
    ev: list[dict[str, Any]] = []
    ml = os.path.join(dd, "meta_launch.db")
    if os.path.isfile(ml):
        c = _ro(ml)
        try:
            for mint, meta, at, sym, name, ok, sh, fj in c.execute(
                    "SELECT mint, meta, matched_at, symbol, name, shield_ok, shield_json, features_json FROM meta_matches "
                    "ORDER BY matched_at DESC LIMIT 6"):
                f = _j(fj)
                why = _j(sh)
                ev.append({"agent": "meta-launch", "at": at, "subject": f"{sym or name or mint[:6]} · meta '{meta}'",
                           "ref": mint, "decision": "enter" if ok else "skip",
                           "reason": "shield passed" if ok else "failed shield: " + "; ".join(why if isinstance(why, list) else [str(why)])[:120],
                           "features": {k: f.get(k) for k in ("meta_score", "sim", "x_ca", "smart_buyers", "top_holders") if k in f}})
            for mint, at, status, reason, ret, closed in c.execute(
                    "SELECT mint, opened_at, status, reason, realized_ret, closed_at FROM meta_positions ORDER BY "
                    "COALESCE(closed_at, opened_at) DESC LIMIT 4"):
                ev.append({"agent": "meta-launch", "at": closed or at, "subject": mint[:6] + "…", "ref": mint,
                           "decision": "paper exit" if closed else "paper entry", "reason": reason or status,
                           "features": {"ret": ret}})
        finally:
            c.close()
    tm = os.path.join(dd, "timing.db")
    if os.path.isfile(tm):
        c = _ro(tm)
        try:
            seen = set()
            for at, aid, mint, act, dj in c.execute(
                    "SELECT at, agent_id, mint, action, detail_json FROM t_decisions ORDER BY id DESC LIMIT 60"):
                if (aid, mint) in seen or len(seen) >= 3:
                    continue
                seen.add((aid, mint))
                d = _j(dj)
                f = d.get("f") or {}
                ev.append({"agent": f"timing {aid[:6]}", "at": _iso(at), "subject": mint[:6] + "…", "ref": mint,
                           "decision": {"wait": "skip"}.get(act, act), "reason": d.get("why") or
                           ("no entry trigger yet" if act == "wait" else act),
                           "features": {k: f.get(k) for k in ("r5", "ofi", "vol_ratio", "slip", "impulse") if k in f}})
        finally:
            c.close()
    sm = smart_section(smart_db) if os.path.isfile(smart_db) else {}
    from .smart_wallets import latest_report
    rep = latest_report(smart_db) or {}
    for s in ((rep.get("smart_follow") or {}).get("recent") or [])[-4:]:
        ev.append({"agent": "smart-follow", "at": s["at"], "subject": f"{s['mint'][:6]}… copying {s['wallet'][:4]}…",
                   "ref": s["mint"], "decision": "paper exit", "reason": f"{s['exit']} after {s['hold_s']}s · net ${s['net']}",
                   "features": {"entry_mcap": s["entry_mcap"], "wallet_ci_lo": s["x"].get("wallet_ci_lo")}})
    for m in (sm.get("moves") or [])[:3]:
        ev.append({"agent": "smart-follow", "at": m["at"], "subject": f"{m['wallet'][:4]}… {m['side']} {m['sol']} SOL",
                   "ref": m["mint"], "decision": "watch" if not m["vetted"] else "evaluate",
                   "reason": "wallet not vetted (insufficient prior closed record)" if not m["vetted"]
                   else "vetted wallet move → entry gates (age ≤15m, mcap 15–500 SOL, 2s latency)",
                   "features": {"mcap_sol": m.get("mcap_sol")}})
    av = os.path.join(dd, "edge_avenues.db")
    if os.path.isfile(av):
        c = _ro(av)
        try:
            for kind, i, at, net, xj in c.execute(
                    "SELECT kind, id, at, net, x_json FROM paper_outcomes p WHERE at=(SELECT MAX(at) FROM paper_outcomes q "
                    "WHERE q.kind=p.kind) AND kind!='smart_follow'"):
                x = _j(xj)
                ev.append({"agent": kind, "at": at, "subject": str(i)[:40], "ref": i, "decision": "paper result",
                           "reason": f"net ${net:+.3f}", "features": {k: x[k] for k in list(x)[:4]}})
        finally:
            c.close()
    c = _ro(main_db)
    try:
        r = c.execute("SELECT MAX(at) FROM xv_gap_history").fetchone()
        if r and r[0]:
            for at, pm, km, verdict, gap, cost, edge in c.execute(
                    "SELECT at, pm_market, kalshi_market, verdict, raw_gap, cost, edge FROM xv_gap_history WHERE at=? "
                    "ORDER BY edge DESC LIMIT 4", (r[0],)):
                e = float(edge or 0)
                ev.append({"agent": "xv-arb", "at": at, "subject": f"{pm[:28]} ↔ {km[:24]}", "ref": km,
                           "decision": "enter (paper)" if e >= 0.01 and verdict == "same" else "skip",
                           "reason": (f"edge {e * 100:.1f}¢ clears 1¢ fee+buffer gate" if e >= 0.01 else
                                      f"below fee+buffer gate (edge {e * 100:.1f}¢ < 1¢)") +
                                     ("" if verdict == "same" else f" · pair {verdict}"),
                           "features": {"raw_gap": gap, "cost": cost}})
    finally:
        c.close()
    pm = os.path.join(dd, "pm_train.db")
    if os.path.isfile(pm):
        c = _ro(pm)
        try:
            r = c.execute("SELECT MAX(snap_ts) FROM pm_near").fetchone()
            for t, lead, ts, bid, ask, mid, vol, dm in c.execute(
                    "SELECT ticker, lead_s, snap_ts, bid, ask, mid, vol10, dmid10 FROM pm_near WHERE snap_ts=? "
                    "ORDER BY vol10 DESC LIMIT 3", (r[0] if r else 0,)):
                ev.append({"agent": "near-close", "at": _iso(ts), "subject": t, "ref": t, "decision": "skip",
                           "reason": "shadow: crypto near-close models not beating market Brier (CI spans 0)",
                           "features": {"lead_s": lead, "bid": bid, "ask": ask, "vol10": vol}})
        finally:
            c.close()
    ev.sort(key=lambda e: e.get("at") or "", reverse=True)
    return {"as_of": ev[0]["at"] if ev else None, "cadence_s": 60, "events": ev[:40]}


# ---- treasury / PM history / near-close / cross-venue -------------------------------
def treasury_section(db: str, now: float | None = None) -> dict[str, Any]:
    """Each venue is read live from its own source every cycle; age = time since ITS last successful read.

    A source that failed this cycle is shown as error/stale with its last good number and age, never as fresh.
    """
    now = now or time.time()
    c = _ro(db)
    try:
        rows = c.execute("SELECT at, status, total_usd, sources_json FROM equity_feed_readings "
                         "ORDER BY rowid DESC LIMIT 1440").fetchall()
        dep = c.execute("SELECT COALESCE(SUM(CAST(deposit_usd AS REAL)),0) FROM equity_feed_readings").fetchone()[0]
        try:
            deps = [{"at": a, "source": s, "usd": float(u)} for a, s, u in c.execute(
                "SELECT at, source, amount_usd FROM equity_feed_deposits ORDER BY at")]
        except sqlite3.OperationalError:
            deps = []
    finally:
        c.close()
    if not rows:
        return {"as_of": None, "cadence_s": 60}
    at, status, total, sj = rows[0]
    from .treasury import label_deposits
    flows = label_deposits(deps)
    bonus = sum(d["usd"] for d in flows if d["kind"] == "bonus")
    cur = _j(sj)
    venues = []
    for k, v in cur.items():
        st = v.get("status")
        if st == "excluded":
            venues.append({"venue": k, "status": "excluded", "usd": None, "last_ok_at": None, "age_s": None,
                           "fresh": "excluded"})
            continue
        last_ok, usd = (v.get("read_at") or at, v.get("equity_usd")) if st == "ok" else (None, None)
        if st != "ok":
            for r in rows[1:]:
                pv = _j(r[3]).get(k) or {}
                if pv.get("status") == "ok":
                    last_ok, usd = pv.get("read_at") or r[0], pv.get("equity_usd")
                    break
        age = now - _ts(last_ok) if last_ok else None
        fresh = "red" if st != "ok" or age is None or age > 360 else "amber" if age > 150 else "ok"
        venues.append({"venue": k, "status": st, "error": v.get("error"), "usd": float(usd) if usd else None,
                       "last_ok_at": last_ok, "age_s": round(age, 1) if age is not None else None,
                       "attempt_at": v.get("read_at") or at, "latency_ms": v.get("latency_ms"), "fresh": fresh,
                       "holdings": {h[:6]: q for h, q in (v.get("holdings") or {}).items()}})
    return {"as_of": at, "cadence_s": 60, "status": status, "total_usd": float(total) if total else None,
            "net_deposits_usd": round(dep, 4), "bonus_usd": round(bonus, 4),
            "owner_deposits_usd": round(dep - bonus, 4), "deposits": flows, "venues": venues}


def pm_history_section(dd: str) -> dict[str, Any]:
    out: dict[str, Any] = {"cadence_s": 3600}
    p = os.path.join(dd, "pmus_history.db")
    if os.path.isfile(p):
        c = _ro(p)
        try:
            out["polymarket_us"] = {"markets": c.execute("SELECT COUNT(*) FROM pmus_markets").fetchone()[0],
                                    "quotes": c.execute("SELECT MAX(rowid) FROM pmus_quotes").fetchone()[0],
                                    "settled": c.execute("SELECT COUNT(*) FROM pmus_settlements").fetchone()[0],
                                    "by_category": dict(c.execute("SELECT category, COUNT(*) FROM pmus_markets GROUP BY 1 "
                                                                  "ORDER BY 2 DESC LIMIT 8").fetchall())}
            out["as_of"] = c.execute("SELECT MAX(at) FROM pmus_quotes").fetchone()[0]
        finally:
            c.close()
    p = os.path.join(dd, "pm_train.db")
    if os.path.isfile(p):
        c = _ro(p)
        try:
            out["kalshi_train"] = {"rows": c.execute("SELECT MAX(rowid) FROM pm_train").fetchone()[0],
                                   "by_category": dict(c.execute("SELECT category, COUNT(*) FROM pm_train GROUP BY 1 "
                                                                 "ORDER BY 2 DESC LIMIT 8").fetchall())}
        finally:
            c.close()
    return out


def near_close_section(dd: str) -> dict[str, Any]:
    p = os.path.join(dd, "pm_train.db")
    if not os.path.isfile(p):
        return {"as_of": None, "cadence_s": 300}
    c = _ro(p)
    try:
        ts = c.execute("SELECT MAX(snap_ts) FROM pm_near").fetchone()[0]
        rows = c.execute("SELECT ticker, lead_s, bid, ask, mid, vol10, dmid10 FROM pm_near WHERE snap_ts=? ORDER BY vol10 "
                         "DESC LIMIT 10", (ts,)).fetchall()
        rep = c.execute("SELECT at, report_json FROM pm_crypto_reports ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        c.close()
    rj = _j(rep[1]) if rep else {}
    return {"as_of": _iso(ts), "cadence_s": 300,
            "rows": [dict(zip(("ticker", "lead_s", "bid", "ask", "mid", "vol10", "dmid10"), r, strict=True)) for r in rows],
            "report_at": rep[0] if rep else None,
            "report": {k: rj[k] for k in list(rj)[:6] if not isinstance(rj[k], (list, dict))}}


def xv_section(db: str) -> dict[str, Any]:
    c = _ro(db)
    try:
        at = c.execute("SELECT MAX(at) FROM xv_gap_history").fetchone()[0]
        rows = c.execute("SELECT pm_market, kalshi_market, verdict, skew_s, raw_gap, cost, edge FROM xv_gap_history WHERE "
                         "at=? ORDER BY edge DESC LIMIT 10", (at,)).fetchall() if at else []
    finally:
        c.close()
    return {"as_of": at, "cadence_s": 900, "rows": [
        {"pm": a, "kalshi": b, "verdict": v, "skew_s": s, "raw_gap": g, "cost": co, "edge": e,
         "gate": "clears 1¢ gate" if (e or 0) >= 0.01 else "below 1¢ fee+buffer gate"} for a, b, v, s, g, co, e in rows]}


def build_live(main_db: str) -> dict[str, Any]:
    now = time.time()
    dd = os.path.dirname(os.path.abspath(main_db))
    smart_db = os.getenv("NOEMA_SMART_DB", os.path.join(dd, "smart_wallets.db"))
    t0 = time.perf_counter()
    out = {"schema": "tev.live.v1", "paper_only": True, "at": _iso(now),
           "launch": _cached("launch", 2, lambda: launch_section(main_db, now)),
           "candles": _cached("candles", 8, lambda: candles_section(main_db, now)),
           "smart": _cached("smart", 20, lambda: smart_section(smart_db)),
           "agents": _cached("agents", 30, lambda: agents_section(dd)),
           "looking": _cached("looking", 4, lambda: looking_section(dd, main_db, smart_db, now)),
           "treasury": _cached("treasury", 5, lambda: treasury_section(main_db, now)),
           "pm_history": _cached("pmh", 300, lambda: pm_history_section(dd)),
           "near_close": _cached("near", 30, lambda: near_close_section(dd)),
           "xv": _cached("xv", 30, lambda: xv_section(main_db))}
    for v in out.values():
        if isinstance(v, dict):
            v.setdefault("as_of", None)
    out["build_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out
