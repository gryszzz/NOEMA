"""Proven-only sizing: money follows measured results after costs, nothing else.

Evidence comes from two places NOEMA already records:

1. **Opportunity history** (``opportunity_feed.db``): every find with its reference price when
   first seen and the price afterwards. Grouped by ``source + reason type`` (numbers stripped
   from the reason), each item's return is the move since found *minus a round-trip cost* for
   that price kind. Non-directional finds (e.g. X bursts with no stance) and locked arbs are not
   scored here - arbs prove themselves on settlement in the strategy book.
2. **Agent forecast scores** (``accuracy_forecasts`` / ``accuracy_resolutions``): mean EV after
   cost per agent. With enough resolved forecasts, a negative score vetoes size.

A bucket is **proven** only with n >= ``NOEMA_PROVEN_MIN_N`` (30) and a one-sided lower bound
(mean - z*se, z = ``NOEMA_PROVEN_Z`` 1.0) above zero. A strategy gets real paper size only if it
is proven (its own settled book is "active", or a mapped feed bucket is proven) and has no veto.
Everything else is ``probe`` (tiny size so results keep accruing) or ``shadow``
(``NOEMA_UNPROVEN_MODE=shadow``). This module never touches live flags or the capital guard;
the guard's floor and daily stop still apply on top of anything sized here.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# round-trip costs per price kind (fraction for ratio kinds, probability points for markets)
COSTS = {"market": 0.03, "token": 0.012, "culture_ratio": 0.004}

STRATEGY_SOURCES = {
    "pm_arb": ["pm_arb"], "news_latency": ["news_latency"], "smart_money": ["alpha"],
    "meme_catalyst": ["alpha", "trench"], "fav_longshot": ["strategy"], "majors_momentum": ["strategy"],
}
SOURCE_AGENTS = {"alpha": ["alpha"], "culture": ["culture"], "trench": ["trench_rules"],
                 "x_burst": ["x_burst"], "news_latency": ["x_burst"]}


def _env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def reason_type(source: str, reason: str | None) -> str:
    head = (reason or "").split(" · ")[0].lower()
    head = re.sub(r"[$€]?[-+]?\d[\d,.]*%?", "#", head)
    head = re.sub(r"\s+", " ", head).strip()[:60]
    return f"{source}:{head or 'unspecified'}"


def item_return(price_kind: str | None, ref: float | None, last: float | None, side: str | None = None) -> float | None:
    """Return after a round-trip cost, or None when the find is not scoreable."""
    if ref is None or last is None or ref <= 0:
        return None
    if price_kind == "market":
        if side not in ("yes", "no"):
            return None  # non-directional: no position would have been taken
        move = (last - ref) if side == "yes" else (ref - last)
        return move - COSTS["market"]
    if price_kind == "token":
        return last / ref - 1 - COSTS["token"]
    if price_kind == "culture" and ref > 1:
        return last / ref - 1 - COSTS["culture_ratio"]
    return None


def summarize(rets: list[float]) -> dict[str, Any]:
    n = len(rets)
    if not n:
        return {"n": 0, "mean": None, "lower": None}
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    lower = mean - _env("NOEMA_PROVEN_Z", 1.0) * (sd / math.sqrt(n) if n > 1 else float("inf"))
    return {"n": n, "mean": round(mean, 5), "lower": None if math.isinf(lower) else round(lower, 5),
            "hit_rate": round(sum(r > 0 for r in rets) / n, 3)}


def bucket_scores(feed_path: str) -> dict[str, dict[str, Any]]:
    if not Path(feed_path).exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{feed_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = c.execute("SELECT source, reason, price_kind, ref_price, last_price, payload_json, status "
                         "FROM opp_items").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        c.close()
    rets: dict[str, list[float]] = {}
    seen: dict[str, int] = {}
    for source, reason, kind, ref, last, payload, status in rows:
        key = reason_type(source, reason)
        seen[key] = seen.get(key, 0) + 1
        side = None
        if payload and '"side"' in payload:
            m = re.search(r'"side":\s*"(yes|no)"', payload)
            side = m.group(1) if m else None
        if status != "expired":
            continue  # only finished observations count
        r = item_return(kind, ref, last, side)
        if r is not None:
            rets.setdefault(key, []).append(r)
    min_n = int(_env("NOEMA_PROVEN_MIN_N", 30))
    out = {}
    for key, count in seen.items():
        s = summarize(rets.get(key, []))
        s["seen"] = count
        s["proven"] = bool(s["n"] >= min_n and s["lower"] is not None and s["lower"] > 0)
        s["source"] = key.split(":", 1)[0]
        out[key] = s
    return out


def agent_scores(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute("SELECT f.agent_id, COUNT(*), AVG(r.ev_after_cost), AVG(r.brier) FROM accuracy_forecasts f "
                            "JOIN accuracy_resolutions r ON r.forecast_id=f.id GROUP BY f.agent_id").fetchall()
    except sqlite3.Error:
        return {}
    return {a: {"n": n, "ev_after_cost": ev, "brier": b} for a, n, ev, b in rows}


def strategy_proof(conn: sqlite3.Connection, feed_path: str | None = None) -> dict[str, dict[str, Any]]:
    """Per strategy: tier ``proven`` / ``probe`` / ``shadow`` / ``benched`` and the evidence."""
    if feed_path is None:
        db = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "") or ""
        feed_path = os.getenv("NOEMA_OPP_FEED_DB") or (str(Path(db).with_name("opportunity_feed.db")) if db else "")
    buckets = bucket_scores(feed_path) if feed_path else {}
    agents = agent_scores(conn)
    min_n = int(_env("NOEMA_PROVEN_MIN_N", 30))
    unproven = "shadow" if os.getenv("NOEMA_UNPROVEN_MODE", "probe") == "shadow" else "probe"
    try:
        alloc = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT strategy, status, reason FROM strategy_alloc")}
    except sqlite3.Error:
        alloc = {}
    out = {}
    for st, sources in STRATEGY_SOURCES.items():
        status, _ = alloc.get(st, ("exploring", None))
        mine = {k: v for k, v in buckets.items() if v["source"] in sources}
        proven_b = [k for k, v in mine.items() if v["proven"]]
        agent_ids = [f"strategy:{st}"] + [a for s in sources for a in SOURCE_AGENTS.get(s, [])]
        vetoes = [a for a in agent_ids if a in agents and agents[a]["n"] >= min_n
                  and (agents[a]["ev_after_cost"] or 0) <= 0]
        if status == "benched":
            tier, why = "benched", "allocator benched it (negative result or failed backtest)"
        elif vetoes:
            tier, why = unproven, f"forecast score negative after costs: {', '.join(vetoes)}"
        elif status == "active" or proven_b:
            tier, why = "proven", ("own settled book positive" if status == "active" else
                                   f"proven bucket(s): {', '.join(proven_b)}")
        else:
            best = max(mine.values(), key=lambda v: v["n"], default=None)
            why = (f"unproven: best bucket n={best['n']}/{min_n}" if best else "unproven: no scored history yet")
            tier = unproven
        out[st] = {"tier": tier, "why": why, "buckets": mine, "vetoes": vetoes,
                   "agents": {a: agents[a] for a in agent_ids if a in agents}}
    return out


def board(db_path: str) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {"strategies": {}}
    try:
        feed = os.getenv("NOEMA_OPP_FEED_DB") or str(Path(db_path).with_name("opportunity_feed.db"))
        proof = strategy_proof(conn, feed)
        buckets = bucket_scores(feed)
    finally:
        conn.close()
    top = sorted(buckets.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:20]
    return {"as_of": datetime.now(UTC).isoformat(), "min_n": int(_env("NOEMA_PROVEN_MIN_N", 30)),
            "strategies": {k: {x: v[x] for x in ("tier", "why", "vetoes")} for k, v in proof.items()},
            "buckets": [{"key": k, **v} for k, v in top]}
