"""Read-only v6 'TEV' trading room extras: crawler lanes, EVM balances, signer status,
provider health, prediction-market board, accuracy leaderboard passthrough.

Pure read model. Never places orders, never touches keys; only reports whether a
signer flag is enabled. EVM balances come from public JSON-RPC (eth_getBalance).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .news_crawler import FEEDS, recent_hits
from .room_v4 import _cached
from .x_signal import x_wire

STARTED_AT = datetime.now(UTC)
SOLANA_ADDRESS = "J57oJ1GAraQuts7htkrRN7mzhkq93sqM3iRKMRuHn5sv"
EVM_ADDRESS = "0x92B9a0C66a37fE9635e55045b3bf40cB035a59C8"
EVM_RPCS = {
    "ethereum": ("ETH", "https://ethereum-rpc.publicnode.com"),
    "base": ("ETH", "https://base-rpc.publicnode.com"),
    "polygon": ("POL", "https://polygon-bor-rpc.publicnode.com"),
}
# Endpoints the crawlers really call (see solana_research.py, sync.py, prediction_venues.py).
LANE_ENDPOINTS = {
    "solana_dex": ["https://api.geckoterminal.com/api/v2/networks/solana/pools",
                   "https://api.dexscreener.com/latest/dex/tokens",
                   "https://frontend-api-v3.pump.fun/coins"],
    "kalshi": ["https://api.elections.kalshi.com/trade-api/v2/markets"],
    "polymarket": ["https://api.polymarket.us/v1/markets"],
}
SIGNER_FLAGS = {"solana": "NOEMA_SOLANA_SIGNER_ENABLED", "evm": "NOEMA_EVM_SIGNER_ENABLED"}


def _ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)


def _q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _evm_balance(chain: str) -> dict[str, Any]:
    unit, url = EVM_RPCS[chain]
    r = httpx.post(url, timeout=6, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                                         "params": [EVM_ADDRESS, "latest"]})
    wei = int(r.json()["result"], 16)
    return {"chain": chain, "unit": unit, "balance": wei / 1e18, "status": "ok",
            "observed_at": datetime.now(UTC).isoformat()}


def evm_balances(fetch: bool = True) -> list[dict[str, Any]]:
    if not fetch:
        return [{"chain": c, "unit": u, "balance": None, "status": "not fetched"}
                for c, (u, _) in EVM_RPCS.items()]
    out = []
    for chain, (unit, _) in EVM_RPCS.items():
        v = _cached(f"evm:{chain}", 120, lambda c=chain: _evm_balance(c))
        out.append({"chain": chain, "unit": unit, **v} if "chain" not in v else v)
    return out


def signer_status() -> list[dict[str, Any]]:
    halt = os.getenv("NOEMA_MASTER_HALT", "").strip()
    return [{"family": fam, "flag": flag,
             "state": "ARMED" if os.getenv(flag, "0").strip() == "1" else "OFF",
             "address": SOLANA_ADDRESS if fam == "solana" else EVM_ADDRESS}
            for fam, flag in SIGNER_FLAGS.items()] + [
        {"family": "master_halt", "flag": "NOEMA_MASTER_HALT", "state": halt or "unset"}]


def crawler_lanes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    since = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    lanes = []
    for key, venue, label in (("kalshi", "kalshi:production", "KALSHI MARKETS"),
                              ("polymarket", "polymarket-us", "POLYMARKET US")):
        last = _q(conn, "SELECT created_at, market_id, snapshot_json FROM forecast_ledger "
                  "WHERE venue=? ORDER BY id DESC LIMIT 40", (venue,))
        n10 = _q(conn, "SELECT COUNT(*) FROM forecast_ledger WHERE venue=? AND created_at>=?",
                 (venue, since))
        total = _q(conn, "SELECT COUNT(DISTINCT market_id) FROM forecast_ledger WHERE venue=?", (venue,))
        recent, seen = [], set()
        for ts, mid, snap in last:
            if mid in seen:
                continue
            seen.add(mid)
            try:
                title = (json.loads(snap) or {}).get("title")
            except ValueError:
                title = None
            recent.append({"ts": ts, "market_id": mid, "title": title})
        lanes.append({"lane": key, "label": label, "endpoints": LANE_ENDPOINTS[key],
                      "last_hit_at": last[0][0] if last else None,
                      "hits_10m": n10[0][0] if n10 else 0,
                      "markets_seen": total[0][0] if total else 0, "recent": recent,
                      "status": "active" if n10 and n10[0][0] else ("idle" if last else "no data")})
    cyc = _q(conn, "SELECT ts, payload_json FROM trench_desk_events WHERE event='desk_cycle' "
             "ORDER BY id DESC LIMIT 6")
    pools10 = 0
    for ts, payload in cyc:
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        if ts >= (datetime.now(UTC) - timedelta(minutes=10)).isoformat():
            pools10 += int(p.get("pools_seen") or 0)
    lanes.insert(0, {"lane": "solana_dex", "label": "SOLANA DEX", "endpoints": LANE_ENDPOINTS["solana_dex"],
                     "last_hit_at": cyc[0][0] if cyc else None, "hits_10m": pools10,
                     "status": "active" if pools10 else ("idle" if cyc else "no data")})
    lanes.append(news_lane(db_path_of(conn)))
    lanes.append(x_lane(db_path_of(conn)))
    return lanes


def db_path_of(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row[2]) if row else ""


def news_lane(db_path: str) -> dict[str, Any]:
    hits = recent_hits(db_path, 30)
    return {"lane": "evidence", "label": "NEWS / EVIDENCE", "endpoints": [f[2] for f in FEEDS] +
            ["https://api.gdeltproject.org/api/v2/doc/doc"],
            "last_hit_at": hits.get("last_fetch_at"), "hits_10m": None,
            "status": "active" if hits.get("articles") else "no data",
            "articles": hits.get("articles", 0), "matched_articles": hits.get("matched_articles", 0),
            "markets_linked": hits.get("markets_linked", 0), "by_source": hits.get("by_source", {}),
            "sources": hits.get("sources", []), "targets": hits.get("targets", []),
            "sources_configured": len(FEEDS),
            "recent": [{"ts": h["published_at"], "title": h["headline"], "market_id": h["market"],
                        "url": h["url"], "source": h["source"], "venue": h["venue"],
                        "relevance": h["relevance"], "category": h.get("category"),
                        "reliability": h.get("reliability"), "weight": h.get("weight")}
                       for h in hits.get("hits", [])],
            "note": None if hits.get("articles") else "news crawler has not stored any articles yet"}


def x_lane(db_path: str) -> dict[str, Any]:
    xw = x_wire(db_path)
    return {"lane": "x", "label": "X WIRE (social)", "endpoints": ["x-inbox (Grok Bot X read tools)"],
            "last_hit_at": xw.get("last_ingest_at"), "hits_10m": None, "status": xw.get("status", "no data"),
            "posts_total": xw.get("posts_total", 0), "pump_flagged": xw.get("pump_flagged", 0),
            "author_mix": xw.get("author_mix", {}), "last_run": xw.get("last_run"),
            "topics": xw.get("topics", []), "recent": xw.get("posts", []), "effects": xw.get("effects", []),
            "role": xw.get("role"), "note": xw.get("note")}


def provider_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = ("provider", "state", "last_attempt_at", "last_success_at", "consecutive_failures",
            "next_retry_at", "last_error_class")
    return [dict(zip(cols, r)) for r in _q(conn, f"SELECT {', '.join(cols)} FROM trench_provider_health "
                                                 "ORDER BY provider")]


def _accuracy_rows(command_center: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Prefer an explicit list, then the accuracy-engine report's agents."""
    explicit = command_center.get("accuracy_leaderboard")
    if isinstance(explicit, list):
        return explicit
    report = command_center.get("accuracy")
    if isinstance(report, dict) and isinstance(report.get("agents"), list):
        return report["agents"]
    ledger_board = (command_center.get("ledger") or {}).get("leaderboard")
    return ledger_board if isinstance(ledger_board, list) else None


def capital_panel(db_path: str) -> dict[str, Any]:
    """Capital guard state plus the latest equity-feed reading. Read-only."""
    from .capital_guard import CapitalGuard, daily_stop_fraction, max_equity_age
    from .equity_feed import latest_reading

    out: dict[str, Any] = {"guard": None, "feed": latest_reading(db_path)}
    if not Path(db_path).exists():
        return out
    try:
        state = CapitalGuard(db_path).state()
    except sqlite3.Error:
        state = None
    if state is not None:
        now = datetime.now(UTC)
        day_start = state.day_start_equity if state.day == now.date().isoformat() else state.equity
        out["guard"] = {
            "equity_usd": str(state.equity), "peak_usd": str(state.peak_equity),
            "floor_usd": str(state.floor), "day_start_usd": str(day_start),
            "daily_stop_usd": str(day_start * (1 - daily_stop_fraction())),
            "tripped": state.tripped, "trip_reason": state.trip_reason,
            "updated_at": state.updated_at.isoformat(),
            "fresh": now - state.updated_at <= max_equity_age(),
        }
    return out


def build_room_v6(db_path: str, *, fetch: bool = True,
                  command_center: dict[str, Any] | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    out: dict[str, Any] = {
        "as_of": now.isoformat(), "paper": True,
        "uptime_seconds": round((now - STARTED_AT).total_seconds()),
        "addresses": {"solana": SOLANA_ADDRESS, "evm": EVM_ADDRESS},
        "evm_balances": evm_balances(fetch), "signers": signer_status(),
        "lanes": [], "providers": [],
        "rate_limits": {"geckoterminal_per_min": 10, "note": "shared limiter across desks"},
        "news_crawler": "not run yet",
    }
    cc = command_center or {}
    out["accuracy_leaderboard"] = _accuracy_rows(cc)
    out["brier_by_model"] = (cc.get("ledger") or {}).get("brier")
    out["capital"] = capital_panel(db_path)
    out["culture"], out["alpha"] = None, None
    if Path(db_path).exists():
        conn = _ro(db_path)
        try:
            out["lanes"] = crawler_lanes(conn)
            out["providers"] = provider_health(conn)
            from .alpha_discovery import alpha_board
            from .culture_crawler import culture_radar

            out["culture"] = culture_radar(db_path)
            out["alpha"] = alpha_board(db_path)
            ev = next((lane for lane in out["lanes"] if lane["lane"] == "evidence"), {})
            if ev.get("articles"):
                out["news_crawler"] = (f"{ev['articles']} articles, {ev['matched_articles']} matched "
                                       f"to {ev['markets_linked']} markets")
        finally:
            conn.close()
    out["generated_ms"] = round((time.time() - now.timestamp()) * 1000)
    return out
