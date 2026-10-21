"""Read-only 'command center' aggregate for the trading room v3.

Every number comes from the local SQLite ledger or a read-only RPC call. Missing
sources are reported as explicit empty/"not wired" states, never synthesized.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .accuracy_api import build_accuracy_report

LADDER = (  # mirrors noema/autonomy.py earned_autonomy thresholds
    {"level": "SHADOW", "resolved": 0, "days": 0},
    {"level": "PAPER", "resolved": 30, "days": 0},
    {"level": "DEMO", "resolved": 100, "days": 7},
    {"level": "MICRO", "resolved": 250, "days": 21, "note": "positive after-cost return, drawdown <= 5%"},
    {"level": "PROVEN", "resolved": 1000, "days": 60},
)
BENCHMARK_MODELS = ("market-baseline-v1", "market-midpoint-v1")


def _j(text: Any) -> Any:
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def _q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _markets(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _q(conn, "SELECT venue, market_id, created_at, opportunity_json, action_json "
                    "FROM forecast_ledger ORDER BY id DESC LIMIT 1500")
    latest: dict[tuple, dict[str, Any]] = {}
    independent: dict[tuple, dict[str, Any]] = {}
    for venue, mid, created, opp, action in rows:
        o, a = _j(opp) or {}, _j(action) or {}
        f, s = o.get("forecast") or {}, o.get("snapshot") or {}
        rec = {"venue": venue, "market_id": mid, "title": s.get("title") or mid, "at": created,
               "yes_bid": s.get("yes_bid"), "yes_ask": s.get("yes_ask"),
               "market_p": o.get("market_probability"), "noema_p": f.get("probability_yes"),
               "model": f.get("model_version"), "edge": o.get("robust_edge"),
               "raw_edge": o.get("raw_edge"), "decision": a.get("decision"), "reason": a.get("reason"),
               "closes_at": s.get("closes_at"), "benchmark": f.get("model_version") in BENCHMARK_MODELS}
        latest.setdefault((venue, mid), rec)
        if not rec["benchmark"]:
            independent.setdefault((venue, mid), rec)
    for venue, mid, created, opp, action in _q(
            conn, "SELECT venue, market_id, created_at, opportunity_json, action_json FROM forecast_ledger "
                  "WHERE json_extract(forecast_json,'$.model_version') NOT IN (?,?) ORDER BY id DESC LIMIT 300",
            BENCHMARK_MODELS):
        if (venue, mid) in independent:
            continue
        o, a = _j(opp) or {}, _j(action) or {}
        f, s = o.get("forecast") or {}, o.get("snapshot") or {}
        independent[(venue, mid)] = {
            "venue": venue, "market_id": mid, "title": s.get("title") or mid, "at": created,
            "market_p": o.get("market_probability"), "noema_p": f.get("probability_yes"),
            "model": f.get("model_version"), "edge": o.get("robust_edge"), "raw_edge": o.get("raw_edge"),
            "decision": a.get("decision"), "reason": a.get("reason")}
    counts = {v: n for v, n in _q(conn, "SELECT venue, COUNT(DISTINCT market_id) FROM market_snapshots GROUP BY venue")}
    watched = list(latest.values())
    by_venue: dict[str, list] = {}
    for r in watched:
        by_venue.setdefault(r["venue"], []).append(r)
    return {"watched_counts": counts,
            "independent": sorted(independent.values(), key=lambda r: -abs(r["edge"] or 0))[:15],
            "latest_by_venue": {v: rs[:12] for v, rs in by_venue.items()}}


def _ledger(conn: sqlite3.Connection) -> dict[str, Any]:
    total = (_q(conn, "SELECT COUNT(*) FROM forecast_ledger") or [(0,)])[0][0]
    by_model = {m or "?": n for m, n in _q(conn, "SELECT json_extract(forecast_json,'$.model_version'), COUNT(*) "
                                               "FROM forecast_ledger GROUP BY 1")}
    resolved = _q(conn, "SELECT f.venue, f.market_id, json_extract(f.forecast_json,'$.model_version'), "
                        "json_extract(f.forecast_json,'$.probability_yes'), o.outcome_yes, o.resolved_at, "
                        "json_extract(f.snapshot_json,'$.title') FROM forecast_ledger f JOIN outcomes o "
                        "ON o.venue=f.venue AND o.market_id=f.market_id WHERE o.outcome_yes IS NOT NULL "
                        "ORDER BY o.resolved_at DESC LIMIT 2000")
    briers: dict[str, list[float]] = {}
    bins = [[0, 0, 0.0] for _ in range(10)]  # n, yes, sum p
    for _, _, model, p, y, _, _ in resolved:
        if p is None:
            continue
        p = float(p)
        briers.setdefault(model or "?", []).append((p - int(y)) ** 2)
        b = bins[min(9, int(p * 10))]
        b[0] += 1; b[1] += int(y); b[2] += p
    outcomes_total = (_q(conn, "SELECT COUNT(*) FROM outcomes WHERE outcome_yes IS NOT NULL") or [(0,)])[0][0]
    return {"forecasts_total": total, "by_model": by_model, "resolved_pairs": len(resolved),
            "outcomes_known": outcomes_total,
            "brier": {m: {"n": len(v), "brier": round(sum(v) / len(v), 4)} for m, v in briers.items()},
            "calibration": [{"bin": i / 10, "n": n, "observed": round(yes / n, 3), "mean_p": round(sp / n, 3)}
                            for i, (n, yes, sp) in enumerate(bins) if n],
            "recent_resolutions": [{"venue": v, "market_id": m, "title": t, "model": mo, "p": p,
                                    "outcome_yes": bool(y), "resolved_at": r}
                                   for v, m, mo, p, y, r, t in resolved[:12]]}


def _coordinator(conn: sqlite3.Connection) -> dict[str, Any]:
    reqs = _q(conn, "SELECT proposal_id, created_at, venue, instrument, side, status, notional_usd "
                    "FROM execution_gateway_requests ORDER BY created_at DESC LIMIT 10")
    quotes = _q(conn, "SELECT venue, market_id, model_version, quoted_at, probability_yes, selected, quote_json "
                      "FROM paper_quotes ORDER BY id DESC LIMIT 10")
    wired = Path(__file__).with_name("coordinator.py").exists()
    return {"coordinator_module_present": wired,
            "status": "wired" if wired else "not wired on this branch (PR #106/#107 pending)",
            "gateway_requests": [dict(zip(("proposal_id", "created_at", "venue", "instrument", "side",
                                           "status", "notional_usd"), r)) for r in reqs],
            "paper_quotes": [{"venue": v, "market_id": m, "model": mv, "quoted_at": q, "p": p,
                              "selected": bool(s), **{k: (_j(js) or {}).get(k) for k in
                                                      ("best_yes_ask", "entry_cost_usd", "expected_net_pnl_usd")}}
                             for v, m, mv, q, p, s, js in quotes]}


def _autonomy(conn: sqlite3.Connection, desks_pnl: float | None) -> dict[str, Any]:
    resolved = (_q(conn, "SELECT COUNT(*) FROM forecast_ledger f JOIN outcomes o ON o.venue=f.venue AND "
                         "o.market_id=f.market_id WHERE o.outcome_yes IS NOT NULL") or [(0,)])[0][0]
    first = (_q(conn, "SELECT MIN(created_at) FROM forecast_ledger") or [(None,)])[0][0]
    days = 0.0
    if first:
        try:
            st = datetime.fromisoformat(str(first))
            st = st if st.tzinfo else st.replace(tzinfo=UTC)
            days = round((datetime.now(UTC) - st).total_seconds() / 86400, 2)
        except ValueError:
            pass
    eq = [(_j(p) or {}).get("equity_usd") for (p,) in _q(
        conn, "SELECT payload_json FROM trench_desk_events WHERE event='equity_snapshot' ORDER BY id")]
    eq = [float(e) for e in eq if e is not None]
    dd, peak = 0.0, None
    for e in eq:
        peak = e if peak is None else max(peak, e)
        if peak:
            dd = max(dd, (peak - e) / peak)
    cur = "SHADOW"
    for rung in LADDER:
        if resolved >= rung["resolved"] and days >= rung["days"]:
            cur = rung["level"]
    nxt = next((r for r in LADDER if r["resolved"] > resolved or r["days"] > days), None)
    return {"ladder": list(LADDER), "current_by_counts": cur, "resolved": resolved, "days": days,
            "next": nxt, "paper_return_usd": desks_pnl, "paper_max_drawdown": round(dd, 4),
            "note": "counts-only view; the authoritative level (API autonomy.level) also requires "
                    "reconciliation, calibration and drawdown gates"}


def _budget(conn: sqlite3.Connection) -> dict[str, Any]:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    cog = (_q(conn, "SELECT COALESCE(SUM(estimated_usd),0), COUNT(*) FROM cognition_budget_reservations "
                    "WHERE day_utc=?", (day,)) or [(0, 0)])[0]
    judge = (_q(conn, "SELECT COALESCE(SUM(estimated_usd),0), COUNT(*) FROM trench_desk_judgements "
                      "WHERE ts >= ?", (day,)) or [(0, 0)])[0]
    return {"day_utc": day, "cognition_usd": round(float(cog[0]), 6), "cognition_calls": cog[1],
            "desk_judgement_usd": round(float(judge[0]), 6), "desk_judgement_calls": judge[1],
            "cognition_cap_usd": _f(os.getenv("NOEMA_COGNITION_MAX_ESTIMATED_USD_PER_DAY")),
            "desk_judgement_cap_usd": _f(os.getenv("NOEMA_DESK_JUDGEMENT_MAX_USD_PER_DAY")),
            "model": os.getenv("NOEMA_OPENAI_MODEL")}


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _research(conn: sqlite3.Connection) -> dict[str, Any]:
    ev = _q(conn, "SELECT source, source_type, observed_at, retrieved_at, evidence_id FROM evidence_records "
                  "ORDER BY retrieved_at DESC LIMIT 12")
    trials = _q(conn, "SELECT family, hypothesis, status, status_updated_at FROM research_trials "
                      "ORDER BY status_updated_at DESC LIMIT 6")
    return {"news_crawler": "not present in NOEMA (evidence is venue history; X chatter is a placeholder)",
            "evidence": [dict(zip(("source", "type", "observed_at", "retrieved_at", "id"), r)) for r in ev],
            "trials": [dict(zip(("family", "hypothesis", "status", "updated_at"), r)) for r in trials]}


def _economy(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = _q(conn, "SELECT created_at, snapshot_json FROM economic_snapshots ORDER BY id DESC LIMIT 1")
    return {"at": row[0][0], **(_j(row[0][1]) or {})} if row else None


def _health(conn: sqlite3.Connection) -> dict[str, Any]:
    hb = _q(conn, "SELECT created_at, cycle_id, health FROM agent_heartbeats ORDER BY id DESC LIMIT 1")
    timings = _q(conn, "SELECT cycle_id, completed_at, duration_seconds, stage_timings_json "
                       "FROM agent_cycle_timings ORDER BY cycle_id DESC LIMIT 30")
    providers = _q(conn, "SELECT provider, state, last_success_at, consecutive_failures, last_error_class "
                         "FROM trench_provider_health ORDER BY provider")
    errors = _q(conn, "SELECT created_at, stage, status, detail FROM runtime_events "
                      "WHERE status NOT IN ('completed','started','ok') ORDER BY id DESC LIMIT 6")
    desk = _q(conn, "SELECT ts FROM trench_desk_events WHERE event='desk_cycle' ORDER BY id DESC LIMIT 1")
    age = None
    if hb:
        try:
            age = round((datetime.now(UTC) - datetime.fromisoformat(hb[0][0])).total_seconds(), 1)
        except ValueError:
            pass
    return {"heartbeat_at": hb[0][0] if hb else None, "heartbeat_age_s": age,
            "cycle_id": hb[0][1] if hb else None, "health": hb[0][2] if hb else "no heartbeat",
            "cycle_durations": [{"cycle": c, "at": a, "s": round(d or 0, 3)} for c, a, d, _ in reversed(timings)],
            "last_stages": _j(timings[0][3]) if timings else None,
            "desk_cycle_at": desk[0][0] if desk else None,
            "providers": [dict(zip(("provider", "state", "last_success_at", "failures", "error"), p))
                          for p in providers],
            "errors": [{"at": a, "stage": s, "status": st, "detail": (d or "")[:160]} for a, s, st, d in errors]}


_TOKENS: dict[str, Any] = {"at": 0.0, "data": None}


def wallet_tokens(address: str | None) -> dict[str, Any]:
    """SPL token balances via read-only JSON-RPC, cached 120s. Never touches keys."""
    if not address:
        return {"status": "no wallet address configured", "tokens": []}
    if _TOKENS["data"] and time.monotonic() - _TOKENS["at"] < 120:
        return _TOKENS["data"]
    import httpx
    urls = [u for u in (os.getenv("NOEMA_SOLANA_RPC_FALLBACK_URL"), os.getenv("NOEMA_SOLANA_RPC_URL")) if u]
    out: dict[str, Any] = {"status": "rpc unavailable", "tokens": [], "address": address}
    for url in urls:
        try:
            toks = []
            for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
                r = httpx.post(url, timeout=4, json={"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                                                     "params": [address, {"programId": program}, {"encoding": "jsonParsed"}]})
                for acc in (r.json().get("result") or {}).get("value", []):
                    info = acc["account"]["data"]["parsed"]["info"]
                    amt = info["tokenAmount"].get("uiAmount") or 0
                    if amt:
                        toks.append({"mint": info["mint"], "amount": amt})
            out = {"status": "ok", "tokens": toks, "address": address,
                   "observed_at": datetime.now(UTC).isoformat()}
            break
        except Exception as exc:  # noqa: BLE001 - read-only panel degrades
            out["status"] = f"rpc error: {type(exc).__name__}"
    _TOKENS.update(at=time.monotonic(), data=out)
    return out


def build_command_center(db_path: str, *, desks_pnl: float | None = None,
                         fetch_tokens: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"as_of": datetime.now(UTC).isoformat()}
    if Path(db_path).exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            data.update(markets=_markets(conn), ledger=_ledger(conn), coordinator=_coordinator(conn),
                        autonomy=_autonomy(conn, desks_pnl), budget=_budget(conn),
                        research=_research(conn), economy=_economy(conn), health=_health(conn))
        finally:
            conn.close()
    else:
        data["missing_db"] = True
    addr = os.getenv("NOEMA_SOLANA_WALLET_ADDRESS")
    data["wallet"] = wallet_tokens(addr) if fetch_tokens else {"status": "skipped", "tokens": []}
    data["accuracy"] = build_accuracy_report(db_path)
    return data
