"""Read-only data for trading room v4: real GeckoTerminal OHLCV for held tokens + CHIEF/SOCIAL log.

Candles are fetched from GeckoTerminal (public, read-only) and cached; nothing is synthesized.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

GT = "https://api.geckoterminal.com/api/v2"
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as exc:  # noqa: BLE001 - chart degrades to an explicit error state
        val = {"status": f"unavailable: {type(exc).__name__}", "candles": []}
    _CACHE[key] = (time.monotonic(), val)
    return val


def _ohlcv(mint: str, symbol: str, timeframe: str, aggregate: int) -> dict[str, Any]:
    headers = {"accept": "application/json"}
    with httpx.Client(timeout=6, headers=headers) as c:
        pools = c.get(f"{GT}/networks/solana/tokens/{mint}/pools", params={"page": 1}).json()
        pool = (pools.get("data") or [{}])[0].get("attributes", {}).get("address")
        if not pool:
            return {"status": "no pool found", "symbol": symbol, "mint": mint, "candles": []}
        data = c.get(f"{GT}/networks/solana/pools/{pool}/ohlcv/{timeframe}",
                     params={"aggregate": aggregate, "limit": 90, "token": mint}).json()
    rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return {"status": "ok", "source": "geckoterminal", "symbol": symbol, "mint": mint, "pool": pool,
            "timeframe": f"{aggregate}{timeframe[0]}", "candles": sorted(rows)}


def _held(db_path: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT mint, symbol, SUM(core_cost_usd + bounce_cost_usd) c FROM trench_desk_positions "
            "GROUP BY mint ORDER BY c DESC LIMIT 2").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [(m, s) for m, s, _ in rows]


def _events(db_path: str, event: str, limit: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute("SELECT ts, payload_json FROM trench_desk_events WHERE event=? "
                            "ORDER BY id DESC LIMIT ?", (event, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    out = []
    for ts, p in rows:
        try:
            out.append({"ts": ts, **json.loads(p)})
        except ValueError:
            pass
    return out


def build_room_v4(db_path: str, *, fetch: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"as_of": datetime.now(UTC).isoformat(), "charts": [], "chief": [],
                           "social": {"status": "unavailable",
                                      "detail": "recycled-X-account check not wired (X search needs login)"}}
    if not Path(db_path).exists():
        return out
    held = _held(db_path)
    if fetch:
        for i, (mint, sym) in enumerate(held):
            tf, agg = ("minute", 5) if i == 0 else ("minute", 15)
            out["charts"].append(_cached(f"{mint}:{agg}", 60, lambda m=mint, s=sym, t=tf, a=agg: _ohlcv(m, s, t, a)))
    out["held"] = [{"mint": m, "symbol": s} for m, s in held]
    out["chief"] = _events(db_path, "chief_decision", 20)
    cyc = _events(db_path, "desk_cycle", 1)
    if cyc and cyc[0].get("social"):
        out["social"] = cyc[0]["social"]
    return out
