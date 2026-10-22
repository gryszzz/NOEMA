"""FRESH-SETUP UNIVERSE for the opportunity feed (paper/watch only).

Not the same large caps over and over: majors and anything above a market-cap ceiling or on the
blocklist (config/setup_universe.json) are excluded from setups. Every setup entity has a novelty
cooldown: once surfaced it stays quiet unless something materially changed (volume or liquidity surge,
big price move, new catalyst). Setups are scored quality x freshness x exit-liquidity. Rug/safety
screens are kept: trench quarantines and alpha screen failures never become setups.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "setup_universe.json"


def load_config() -> dict[str, Any]:
    p = Path(os.getenv("NOEMA_SETUP_UNIVERSE_CONFIG", str(_CFG_PATH)))
    try:
        cfg = json.loads(p.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("mcap_ceiling_usd", 250e6)
    cfg.setdefault("blocklist_symbols", ["BTC", "ETH", "SOL", "USDC", "USDT"])
    cfg.setdefault("cooldown_hours", 6)
    cfg.setdefault("material_change", {"volume_mult": 2.0, "liquidity_mult": 1.5, "price_move": 0.2})
    cfg.setdefault("min_exit_liquidity_usd", 5000)
    cfg.setdefault("new_market_hours", 6)
    cfg.setdefault("min_new_market_liquidity_usd", 200)
    cfg["_block"] = {s.upper() for s in cfg["blocklist_symbols"]}
    return cfg


def is_major(symbol: str | None, mcap: float | None, cap_class: str | None, cfg: dict[str, Any]) -> bool:
    if symbol and symbol.upper().lstrip("$") in cfg["_block"]:
        return True
    if cap_class == "major":
        return True
    return bool(mcap and mcap > cfg["mcap_ceiling_usd"])


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def score(quality: float, age_min: float, liquidity: float | None, cfg: dict[str, Any], half_life_min: float = 45) -> float:
    fresh = math.exp(-max(0.0, age_min) / half_life_min)
    liq = 1.0 if liquidity is None else min(1.0, liquidity / (4 * cfg["min_exit_liquidity_usd"]))
    return round(max(0.0, min(1.0, quality)) * fresh * liq, 4)


SEEN_SCHEMA = """CREATE TABLE IF NOT EXISTS setup_seen (entity TEXT PRIMARY KEY, surfaced_at TEXT, volume REAL,
  liquidity REAL, price REAL, catalyst TEXT)"""


def novel(fc: sqlite3.Connection, ent: str, now: datetime, cfg: dict[str, Any], *, volume: float | None,
          liquidity: float | None, price: float | None, catalyst: str | None) -> str | None:
    """Return a novelty reason ('new' / what changed) or None if still on cooldown."""
    fc.execute(SEEN_SCHEMA)
    row = fc.execute("SELECT surfaced_at, volume, liquidity, price, catalyst FROM setup_seen WHERE entity=?", (ent,)).fetchone()
    why = None
    if row is None:
        why = "new"
    else:
        at, v0, l0, p0, c0 = row
        mc = cfg["material_change"]
        if now - datetime.fromisoformat(at) >= timedelta(hours=cfg["cooldown_hours"]):
            why = "cooldown over"
        elif volume and v0 and volume >= v0 * mc["volume_mult"]:
            why = f"volume x{volume / v0:.1f}"
        elif liquidity and l0 and liquidity >= l0 * mc["liquidity_mult"]:
            why = f"liquidity x{liquidity / l0:.1f}"
        elif price and p0 and abs(price / p0 - 1) >= mc["price_move"]:
            why = f"price {price / p0 - 1:+.0%}"
        elif catalyst and catalyst != c0:
            why = "new catalyst"
    if why:
        fc.execute("INSERT OR REPLACE INTO setup_seen VALUES (?,?,?,?,?,?)",
                   (ent, now.isoformat(), volume, liquidity, price, catalyst))
    return why


def _q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def raw_setups(conn: sqlite3.Connection, now: datetime, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidate fresh setups before novelty filtering."""
    out: list[dict[str, Any]] = []
    # 1) new pools that passed the trench screens with enough liquidity to exit, accelerating flow
    since = (now - timedelta(minutes=30)).isoformat()
    for cid, mint, at, px, liq, score_o, risk, aj in _q(conn,
            "SELECT candidate_id, token_mint, captured_at, reference_price_usd, reference_liquidity_usd, opportunity_score,"
            " survival_risk, assessment_json FROM trench_candidates WHERE captured_at>=? AND disposition='observe'"
            " AND reference_liquidity_usd>=? ORDER BY captured_at DESC LIMIT 200", (since, cfg["min_exit_liquidity_usd"])):
        try:
            feats = json.loads(aj).get("features", {}) or {}
        except (TypeError, ValueError):
            feats = {}
        accel = (_f(feats.get("buyer_acceleration")) or 0) > 0 or (_f(feats.get("liquidity_growth_fraction")) or 0) > 0.1
        q = 0.5 * (_f(score_o) or 0) + 0.5 * (1 - (_f(risk) or 1)) + (0.15 if accel else 0)
        age = (now - datetime.fromisoformat(at)).total_seconds() / 60
        out.append({"entity": f"sol:{mint}", "kind": "new_pool", "title": f"new pool {mint[:6]}…{mint[-4:]}",
                    "venue": "solana", "ref": mint, "price_kind": "trench", "price": _f(px), "liquidity": _f(liq),
                    "volume": None, "catalyst": "accelerating" if accel else None, "at": at,
                    "score": score(q, age, _f(liq), cfg),
                    "why": f"passed pool screens · liq ${(_f(liq) or 0):,.0f}" + (" · flow accelerating" if accel else ""),
                    "url": f"https://dexscreener.com/solana/{mint}"})
    # 2) rug-screened tokens below the cap ceiling, rising volume
    since = (now - timedelta(hours=2)).isoformat()
    for chain, addr, sym, at, sc, cap, px, liq, vol, mcap, ageh, h1, buzz, lg in _q(conn,
            "SELECT chain, address, symbol, evaluated_at, score, cap_class, price, liquidity, volume_24h, mcap, age_hours,"
            " h1, buzz_velocity, liquidity_growth FROM alpha_candidates WHERE passed=1 AND evaluated_at>=?", (since,)):
        if is_major(sym, _f(mcap), cap, cfg) or (_f(liq) or 0) < cfg["min_exit_liquidity_usd"]:
            continue
        q = (_f(sc) or 0) + (0.1 if (_f(h1) or 0) > 0 else 0) + (0.1 if (_f(lg) or 0) > 0.1 else 0)
        age = (now - datetime.fromisoformat(at)).total_seconds() / 60
        out.append({"entity": f"{chain}:{addr}", "kind": "fresh_token", "title": f"{sym} ({chain})", "venue": chain,
                    "ref": addr, "price_kind": "token", "price": _f(px), "liquidity": _f(liq), "volume": _f(vol),
                    "catalyst": "buzz" if (_f(buzz) or 0) > 0 else None, "at": at, "score": score(q, age, _f(liq), cfg),
                    "why": f"rug screens pass · mcap ${(_f(mcap) or 0) / 1e6:,.1f}M · 1h {(_f(h1) or 0):+.1f}%",
                    "url": f"https://dexscreener.com/{chain}/{addr}"})
    # 3) early X mentions before the market moved
    since = (now - timedelta(hours=2)).isoformat()
    for ent, z, venue, mid, title, at, status in _q(conn,
            "SELECT entity, z, venue, market_id, title, at, status FROM x_bursts WHERE at>=? AND status='not_priced_in'",
            (since,)):
        if is_major(ent, None, None, cfg):
            continue
        age = (now - datetime.fromisoformat(at)).total_seconds() / 60
        out.append({"entity": f"x:{ent}:{mid}", "kind": "early_x", "title": f"early X: “{ent}” → {(title or mid or '')[:70]}",
                    "venue": venue, "ref": mid, "price_kind": "market" if mid else None, "price": None, "liquidity": None,
                    "volume": _f(z), "catalyst": f"burst:{at[:13]}", "at": at, "score": score(min(1.0, (_f(z) or 0) / 6), age, None, cfg),
                    "why": f"X burst z={(_f(z) or 0):.1f}, market not repriced yet"})
    # 4) newly listed prediction markets with enough book to trade
    since = (now - timedelta(hours=cfg["new_market_hours"])).isoformat()
    for venue, mid, first, sj in _q(conn,
            "SELECT venue, market_id, MIN(captured_at), snapshot_json FROM market_snapshots WHERE valid=1 GROUP BY venue,"
            " market_id HAVING MIN(captured_at)>=? ORDER BY MIN(captured_at) DESC LIMIT 100", (since,)):
        try:
            s = json.loads(sj)
        except ValueError:
            continue
        liq = _f(s.get("liquidity_usd")) or 0
        ya, yb = _f(s.get("yes_ask")), _f(s.get("yes_bid"))
        if liq < cfg["min_new_market_liquidity_usd"] or ya is None or yb is None:
            continue
        spread = ya - yb
        q = max(0.0, 0.8 - spread * 4) * (1.0 if 0.05 <= ya <= 0.95 else 0.5)
        age = (now - datetime.fromisoformat(first)).total_seconds() / 60
        out.append({"entity": f"mkt:{venue}:{mid}", "kind": "new_market", "title": (s.get("title") or mid)[:90],
                    "venue": venue, "ref": mid, "price_kind": "market", "price": round((ya + yb) / 2, 4),
                    "liquidity": liq, "volume": None, "catalyst": None, "at": first,
                    "score": score(q, age, None, cfg, half_life_min=120),
                    "why": f"newly listed · {ya:.2f}/{yb:.2f} · spread {spread:.2f} · book ${liq:,.0f}"})
    return out


def collect_setups(conn: sqlite3.Connection, fc: sqlite3.Connection, now: datetime, limit: int = 15
                   ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cfg = load_config()
    raw = raw_setups(conn, now, cfg)
    raw.sort(key=lambda r: -r["score"])
    items = []
    cooled = 0
    per_kind: dict[str, int] = {}
    per_kind_cap = max(3, limit // 3)
    for r in raw:
        if len(items) >= limit:
            break
        if per_kind.get(r["kind"], 0) >= per_kind_cap:
            continue
        why = novel(fc, r["entity"], now, cfg, volume=r["volume"], liquidity=r["liquidity"], price=r["price"],
                    catalyst=r["catalyst"])
        live = fc.execute("SELECT 1 FROM opp_items WHERE id=? AND status='live'", (f"setup:{r['entity']}",)).fetchone()
        if not why and not live:
            cooled += 1
            continue
        per_kind[r["kind"]] = per_kind.get(r["kind"], 0) + 1
        items.append({"id": f"setup:{r['entity']}", "tier": 1 if r["score"] >= 0.35 else 2, "title": r["title"],
                      "venue": r["venue"], "ref": r["ref"], "edge": None, "score": r["score"], "source_at": r["at"],
                      "reason": f"setup · {r['kind'].replace('_', ' ')} · {r['why']} · {why or 'still live'}"
                                f" · score {r['score']:.2f}",
                      "price_kind": r["price_kind"], "price": r["price"], "url": r.get("url")})
    by_kind: dict[str, int] = {}
    for r in raw:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    return items, {"n": len(items), "note": f"fresh setups (majors excluded, {cooled} on cooldown) · raw {by_kind}"}


def finds_per_hour(feed_db: str, hours: int = 6) -> dict[str, Any]:
    """Unique new finds per hour, split into legacy sources vs 'setup'."""
    c = sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT source, first_seen FROM opp_items").fetchall()
    finally:
        c.close()
    out: dict[str, dict[str, int]] = {}
    for src, fs in rows:
        k = "setup" if src == "setup" else "legacy"
        out.setdefault(fs[:13], {}).setdefault(k, 0)
        out[fs[:13]][k] += 1
    return dict(sorted(out.items())[-hours:])
