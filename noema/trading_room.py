"""Read-only 'trading room' aggregate: desks, crawler feed, approval chain, venues, flags.

Pure read model. It never places orders, never returns credential material, and
reports live-execution flags exactly as configured (they are expected to be OFF).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .accuracy_api import build_accuracy_report
from .autonomy import earned_autonomy
from .economic_models import AutonomyEvidence
from .trench_desks import desk_overview

LIVE_FLAGS = (
    "NOEMA_EXECUTION_GATEWAY_ENABLED", "NOEMA_ALLOW_LIVE_ORDERS",
    "NOEMA_TREASURY_ACTIONS_ENABLED", "NOEMA_SOLANA_SIGNER_ENABLED",
)
CHAIN = ("crawler", "filters", "holders", "judgement", "desk")


def live_flags() -> dict[str, Any]:
    flags = {name: os.getenv(name, "0").strip() == "1" for name in LIVE_FLAGS}
    cap = os.getenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "").strip()
    return {"flags": flags, "any_live": any(flags.values()),
            "max_live_daily_notional_usd": cap or None,
            "live_venues": os.getenv("NOEMA_LIVE_VENUES", "").strip() or None}


def _events(conn: sqlite3.Connection, event: str, limit: int) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT ts, payload_json FROM trench_desk_events WHERE event=? ORDER BY id DESC LIMIT ?",
            (event, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for ts, payload in rows:
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        data["ts"] = ts
        out.append(data)
    return out


def _judgements(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT ts, source, status, estimated_usd, candidates_json, result_json "
            "FROM trench_desk_judgements ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for ts, source, status, cost, cands, result in rows:
        symbols = {}
        try:
            for c in json.loads(cands) or []:
                if isinstance(c, dict):
                    symbols[c.get("mint")] = c.get("symbol")
        except ValueError:
            pass
        picks = []
        try:
            parsed = json.loads(result)
        except ValueError:
            parsed = []
        items = parsed if isinstance(parsed, list) else (parsed or {}).get("judgements", [])
        for item in items or []:
            if isinstance(item, dict):
                picks.append({"mint": item.get("mint"), "symbol": symbols.get(item.get("mint")),
                              "trade": bool(item.get("trade")),
                              "probability": item.get("probability")})
        out.append({"ts": ts, "source": source, "status": status,
                    "estimated_usd": round(float(cost or 0), 6), "judgements": picks})
    return out


def autonomy_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Conservative estimate: only counts resolved forecasts and days observed."""
    try:
        resolved = conn.execute(
            "SELECT COUNT(*) FROM forecast_ledger f JOIN outcomes o "
            "ON o.venue=f.venue AND o.market_id=f.market_id WHERE o.outcome_yes IS NOT NULL"
        ).fetchone()[0]
        first = conn.execute("SELECT MIN(created_at) FROM forecast_ledger").fetchone()[0]
    except sqlite3.OperationalError:
        resolved, first = 0, None
    days = 0
    if first:
        try:
            start = datetime.fromisoformat(str(first))
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            days = max(0, (datetime.now(UTC) - start).days)
        except ValueError:
            days = 0
    evidence = AutonomyEvidence(
        resolved_forecasts=int(resolved), live_observation_days=days,
        after_cost_return=Decimal(0), realized_net_pnl_usd=Decimal(0),
        max_drawdown_fraction=Decimal(1), calibration_error=None,
        reconciliation_ok_fraction=Decimal(0), profitable_days=0, losing_days=0)
    decision = earned_autonomy(evidence)
    level = decision.earned_level
    return {"level": str(getattr(level, "value", level)).upper(),
            "resolved_forecasts": int(resolved), "days_observed": days,
            "reasons": list(decision.reasons),
            "note": "conservative: unproven gates (reconciliation, drawdown, calibration) count as failing"}


def _venue_summary(venues: dict[str, Any] | None, wallets: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for v in (venues or {}).get("venues", []) or []:
        acct = v.get("account") or {}
        positions = acct.get("positions")
        if not isinstance(positions, list):
            positions = (acct.get("capital") or {}).get("positions") or []
        out.append({
            "venue": v.get("venue"), "status": acct.get("status", "unknown"),
            "cash_usd": acct.get("cash_balance_usd"),
            "portfolio_value_usd": acct.get("portfolio_value_usd"),
            "positions": [p for p in positions if isinstance(p, dict)][:10]
            if isinstance(positions, list) else [],
            "position_count": len(positions) if isinstance(positions, list) else positions,
            "market_data": (v.get("market_data") or {}).get("status"),
            "observed_at": acct.get("observed_at"), "execution": "disabled",
        })
    for n in (wallets or {}).get("networks", []) or []:
        if n.get("chain") == "solana":
            out.append({"venue": "Solana wallet", "address": n.get("address"),
                        "status": n.get("status"), "sol": n.get("sol"),
                        "usd": n.get("usd_value") or n.get("value_usd"),
                        "observed_at": n.get("observed_at"), "execution": "disabled"})
    return out


def build_trading_room(db_path: str, *, venues: dict[str, Any] | None = None,
                       wallets: dict[str, Any] | None = None) -> dict[str, Any]:
    desks = desk_overview(db_path)
    cycles: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    judgements: list[dict[str, Any]] = []
    autonomy = {"level": "SHADOW", "resolved_forecasts": 0, "days_observed": 0, "reasons": []}
    if Path(db_path).exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cycles = _events(conn, "desk_cycle", 30)
            equity = list(reversed(_events(conn, "equity_snapshot", 500)))
            judgements = _judgements(conn)
            autonomy = autonomy_status(conn)
        finally:
            conn.close()
    latest = cycles[0] if cycles else {}
    by_desk: dict[str, list[dict[str, Any]]] = {}
    for t in desks.get("recent_trades", []):
        by_desk.setdefault(t["desk_id"], []).append(t)
    positions: dict[str, list[dict[str, Any]]] = {}
    for p in desks.get("positions", []):
        mark = p.get("last_mark_price") or 0
        qty = (p.get("core_qty") or 0) + (p.get("bounce_qty") or 0)
        cost = (p.get("core_cost_usd") or 0) + (p.get("bounce_cost_usd") or 0)
        positions.setdefault(p["desk_id"], []).append({
            "symbol": p["symbol"], "mint": p["mint"], "qty": qty, "cost_usd": round(cost, 4),
            "mark_price": mark, "value_usd": round(qty * mark, 4),
            "unrealized_usd": round(qty * mark - cost, 4), "opened_at": p["opened_at"]})
    seats = []
    for d in desks.get("desks", []):
        pos = positions.get(d["desk_id"], [])
        seats.append({**d, "hired": d["status"] == "active",
                      "unrealized_pnl_usd": round(sum(p["unrealized_usd"] for p in pos), 4),
                      "positions_detail": pos, "trades": by_desk.get(d["desk_id"], [])[:6]})
    feed = latest.get("feed") or []
    chain = {
        "crawler": latest.get("pools_seen", 0),
        "filters": latest.get("prefilter_passed", 0),
        "holders": latest.get("entry_passed", 0),
        "judgement": len(latest.get("picks") or []),
        "desk": latest.get("entries", 0),
        "holder_checked": latest.get("holder_checked", 0),
        "rejections": latest.get("rejections") or {},
        "judgement_status": latest.get("judgement"),
        "pumpfun_new_launches": latest.get("pumpfun_new_launches"),
        "ts": latest.get("ts"),
    }
    return {
        "as_of": datetime.now(UTC).isoformat(), "paper": True,
        "flags": live_flags(), "autonomy": autonomy,
        "seats": seats, "total_pnl_after_costs_usd": desks.get("total_pnl_after_costs_usd", 0.0),
        "chain": chain, "feed": feed,
        # real feeds from recent cycles, so the UI can replay genuine log lines between cycles
        "feed_history": [{"cycle_ts": c.get("ts"), "feed": (c.get("feed") or [])[:60]}
                         for c in cycles[:6]],
        "interval_seconds": float(os.getenv("NOEMA_TRENCH_DESK_INTERVAL_SECONDS", "120")), "judgements": judgements,
        "judgement_spend_usd": round(sum(j["estimated_usd"] for j in judgements), 6),
        "equity_series": [{"ts": e["ts"], "equity_usd": e.get("equity_usd"),
                           "pnl_usd": e.get("pnl_after_costs_usd")} for e in equity],
        "cycles": [{k: c.get(k) for k in ("ts", "pools_seen", "prefilter_passed",
                                          "entry_passed", "entries", "fired")} for c in cycles[:10]],
        "events": [e for e in desks.get("recent_events", []) if e.get("event") not in ("desk_cycle", "equity_snapshot")][:15],
        "venues": _venue_summary(venues, wallets),
        "accuracy": build_accuracy_report(db_path),
    }
