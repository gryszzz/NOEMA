"""Read-only treasury board (schema tev.treasury.v1) for the room's live equity chart.

Real money comes only from the read-only equity feed (equity_feed_readings) and the capital
guard state. Paper books are returned in a separate ``paper`` block and are never added to real
totals. Deposits are marked and excluded from PnL.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .capital_guard import daily_stop_fraction, floor_fraction

VENUES = (("solana", "Solana", ("solana",)), ("evm", "EVM (Ethereum/Base/Polygon)", ("evm_",)),
          ("kalshi", "Kalshi", ("kalshi",)), ("polymarket_us", "Polymarket US", ("polymarket_us",)))
MAX_POINTS = 600


def promo_bonuses() -> dict[str, float]:
    """Venue promo credits (e.g. NOEMA_TREASURY_BONUSES="polymarket_us=15"). Counted as funding, never profit."""
    import os
    out: dict[str, float] = {}
    for part in os.getenv("NOEMA_TREASURY_BONUSES", "").split(","):
        k, _, v = part.partition("=")
        if k.strip() and _f(v) is not None:
            out[k.strip()] = out.get(k.strip(), 0.0) + float(v)
    return out


def label_deposits(deposits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split each venue's first deposits into owner deposit vs promo bonus (bonus allocated first-come)."""
    left = promo_bonuses()
    out = []
    for d in deposits:
        usd, src = d.get("usd") or 0.0, d.get("source")
        b = min(max(usd, 0.0), left.get(src, 0.0))
        if b > 0:
            left[src] -= b
            out.append({**d, "usd": round(b, 6), "kind": "bonus", "label": "promo bonus (not profit)"})
        if usd - b != 0:
            out.append({**d, "usd": round(usd - b, 6), "kind": "deposit" if usd - b > 0 else "withdrawal",
                        "label": "deposit" if usd - b > 0 else "withdrawal"})
    return out


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _venues(sources: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, label, prefixes in VENUES:
        parts = {k: v for k, v in sources.items() if any(k == p or (p.endswith("_") and k.startswith(p))
                                                          for p in prefixes)}
        vals = [_f(v.get("equity_usd")) for v in parts.values()]
        ok = all(v.get("status") == "ok" for v in parts.values()) and bool(parts)
        out.append({"key": key, "label": label, "usd": round(sum(x for x in vals if x is not None), 6),
                    "status": "ok" if ok else "partial" if parts else "missing",
                    "parts": {k: {"status": v.get("status"), "usd": _f(v.get("equity_usd"))} for k, v in parts.items()}})
    return out


def treasury_board(db_path: str, *, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {"schema": "tev.treasury.v1", "at": now.isoformat(), "real": None, "series": [],
                           "deposits": [], "guard": None, "paper": {"label": "PAPER (simulated, not real money)",
                                                                    "desks": [], "alpha_book": None}}
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        since = (now - timedelta(days=days)).isoformat()
        try:
            rows = c.execute("SELECT at, total_usd, deposit_usd, status FROM equity_feed_readings WHERE "
                             "total_usd IS NOT NULL AND status='complete' ORDER BY at").fetchall()
            first = rows[0] if rows else None
            recent = [r for r in rows if r[0] >= since]
            step = max(1, len(recent) // MAX_POINTS)
            pts = recent[::step] + ([recent[-1]] if recent and (len(recent) - 1) % step else [])
            out["series"] = [{"at": r[0], "usd": _f(r[1])} for r in pts]
            out["deposits"] = label_deposits([{"at": a, "source": s, "asset": asset, "usd": _f(u)} for a, s, asset, u in
                                              c.execute("SELECT at, source, asset, amount_usd FROM "
                                                        "equity_feed_deposits ORDER BY at")])
            last = c.execute("SELECT at, status, total_usd, sources_json FROM equity_feed_readings "
                             "ORDER BY at DESC LIMIT 1").fetchone()
            lastok = rows[-1] if rows else None
        except sqlite3.OperationalError:
            first = last = lastok = None
        if lastok:
            src = json.loads(c.execute("SELECT sources_json FROM equity_feed_readings WHERE at=?",
                                       (lastok[0],)).fetchone()[0])
            total = _f(lastok[1]) or 0.0
            dep_total = sum(d["usd"] or 0 for d in out["deposits"])
            start = _f(first[1]) or 0.0
            at = datetime.fromisoformat(lastok[0])
            out["real"] = {"total_usd": total, "last_reading_at": lastok[0],
                           "age_s": int((now - at).total_seconds()),
                           "latest_status": last[1] if last else None, "venues": _venues(src),
                           "first_reading_at": first[0], "start_usd": start, "deposits_usd": dep_total,
                           "bonus_usd": round(sum(d["usd"] for d in out["deposits"] if d.get("kind") == "bonus"), 6),
                           "pnl_all_usd": round(total - start - dep_total, 6),
                           "pnl_all_pct": round((total - start - dep_total) / (start + dep_total) * 100, 3)
                           if start + dep_total > 0 else None}
        try:
            g = c.execute("SELECT peak_equity, floor, equity, day, day_start_equity, tripped, trip_reason, "
                          "updated_at FROM capital_guard WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            g = None
        if g:
            ds, stop = float(g[4]), float(daily_stop_fraction())
            eq = float(g[2])
            out["guard"] = {"peak_usd": float(g[0]), "floor_usd": float(g[1]), "equity_usd": eq, "day": g[3],
                            "day_start_usd": ds, "daily_stop_usd": round(ds * (1 - stop), 6),
                            "daily_stop_fraction": stop, "floor_fraction": float(floor_fraction()),
                            "tripped": bool(g[5]), "trip_reason": g[6], "updated_at": g[7],
                            "pnl_today_usd": round(eq - ds, 6),
                            "pnl_today_pct": round((eq - ds) / ds * 100, 3) if ds > 0 else None}
        try:
            snaps = c.execute("SELECT ts, payload_json FROM trench_desk_events WHERE event='equity_snapshot' "
                              "AND ts>=? ORDER BY id", (since,)).fetchall()
            step = max(1, len(snaps) // MAX_POINTS)
            for ts, p in snaps[::step] + ([snaps[-1]] if snaps and (len(snaps) - 1) % step else []):
                j = json.loads(p or "{}")
                out["paper"]["desks"].append({"at": ts, "usd": _f(j.get("equity_usd")),
                                              "pnl_usd": _f(j.get("pnl_after_costs_usd")),
                                              "start_usd": _f(j.get("starting_usd"))})
        except (sqlite3.OperationalError, ValueError):
            pass
        try:
            b = c.execute("SELECT starting_usd, cash_usd, peak_equity, day_start_equity, halted, updated_at "
                          "FROM alpha_book WHERE id=1").fetchone()
            if b:
                out["paper"]["alpha_book"] = {"start_usd": b[0], "cash_usd": b[1], "peak_usd": b[2],
                                              "day_start_usd": b[3], "halted": bool(b[4]), "updated_at": b[5]}
        except sqlite3.OperationalError:
            pass
    finally:
        c.close()
    out["stripe"] = {"note": "Stripe is a read-only business view; it is not trading capital and is not "
                             "counted in the balance."}
    return out
