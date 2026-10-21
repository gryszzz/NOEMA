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

from .room_v4 import _cached

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
    "evidence": ["https://api.elections.kalshi.com/trade-api/v2/markets?status=settled"],
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
    ev = _q(conn, "SELECT source, source_type, retrieved_at, evidence_id FROM evidence_records "
            "ORDER BY rowid DESC LIMIT 8")
    lanes.append({"lane": "evidence", "label": "EVIDENCE / WEB", "endpoints": LANE_ENDPOINTS["evidence"],
                  "last_hit_at": ev[0][2] if ev else None, "hits_10m": None,
                  "recent": [{"ts": r[2], "title": f"{r[0]} ({r[1]})", "market_id": r[3]} for r in ev],
                  "status": "active" if ev else "no data",
                  "note": "no general news/web crawler exists in NOEMA yet; evidence is venue settlement history"})
    return lanes


def provider_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = ("provider", "state", "last_attempt_at", "last_success_at", "consecutive_failures",
            "next_retry_at", "last_error_class")
    return [dict(zip(cols, r)) for r in _q(conn, f"SELECT {', '.join(cols)} FROM trench_provider_health "
                                                 "ORDER BY provider")]


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
        "news_crawler": "not present",
    }
    cc = command_center or {}
    acc = cc.get("accuracy_leaderboard") or cc.get("accuracy") or (cc.get("ledger") or {}).get("leaderboard")
    out["accuracy_leaderboard"] = acc if isinstance(acc, list) else None
    out["brier_by_model"] = (cc.get("ledger") or {}).get("brier")
    if Path(db_path).exists():
        conn = _ro(db_path)
        try:
            out["lanes"] = crawler_lanes(conn)
            out["providers"] = provider_health(conn)
        finally:
            conn.close()
    out["generated_ms"] = round((time.time() - now.timestamp()) * 1000)
    return out
