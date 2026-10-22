"""Kalshi <-> Polymarket US cross-venue matcher (read-only, paper only).

Title similarity alone found 0 pairs: NOEMA's Kalshi crawl is dominated by short-dated props,
while Polymarket US lists season futures whose market titles are just the outcome name
("Atlanta Hawks"). This module matches on structure instead:

* **family** - a Polymarket US event slug prefix mapped to one Kalshi series/event
  (NBA/NFL/NHL champion, NBA/NFL MVP, Best Picture, BTC yearly highs...),
* **entity** - normalised team/person/film name, unique one-to-one or skipped,
* **strike** - numeric threshold must be equal (BTC "Above $109,999.99"),
* **resolution check** - rules-based comparison of both venues' resolution text (season year,
  deadline, price source, touch-vs-close, tie handling). Verdicts: ``equivalent``,
  ``caveat`` (probably the same, a human should read both) or ``mismatch`` (never traded).

Quotes are refreshed together per family: one public Kalshi series call, then the
Polymarket US BBO for exactly the matched slugs right after, so legs are seconds apart.
Gaps are after both venues' official taker fees plus slippage (see ``edge_strategies``).
Nothing here places orders.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from . import edge_strategies as es

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# (polymarket-us slug prefix, kalshi series, kalshi event ticker, kind)
FAMILIES: list[tuple[str, str, str, str]] = [
    ("tec-nba-champ-2027-", "KXNBA", "KXNBA-27", "winner"),
    ("tec-nfl-champ-2027-", "KXSB", "KXSB-27", "winner"),
    ("tec-nhl-champ-2027-", "KXNHL", "KXNHL-27", "winner"),
    ("tec-mlb-champ-2026-", "KXMLB", "KXMLB-26", "winner"),
    ("tec-nba-mvp-2027-", "KXNBAMVP", "KXNBAMVP-27", "award"),
    ("tec-nfl-mvp-2027-", "KXNFLMVP", "KXNFLMVP-27", "award"),
    ("tac-oscars-03-14-2027-bestpic", "KXOSCARPIC", "KXOSCARPIC-27", "award"),
    ("cpc-btc-hitprice-high-yr-12-31-2026", "KXBTCMAXY", "KXBTCMAXY-26DEC31", "strike_touch"),
]


# --------------------------------------------------------------------------- entities
def norm_name(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’]", "", s)
    toks = [t for t in re.split(r"[^a-z0-9]+", s) if t and t not in ("jr", "sr", "ii", "iii", "the", "fc")]
    return toks


def names_match(a: str, b: str) -> bool:
    """Same entity if the shorter token list is a prefix of the longer one, where a single-letter
    token stands for a nickname initial ("Los Angeles C" == "Los Angeles Clippers")."""
    x, y = norm_name(a), norm_name(b)
    if not x or not y:
        return False
    s, l = (x, y) if len(x) <= len(y) else (y, x)
    for t, u in zip(s, l):
        if t == u or (len(t) == 1 and u.startswith(t)) or (len(u) == 1 and t.startswith(u)):
            continue
        return False
    return True


def strike_of(text: str) -> float | None:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


# ------------------------------------------------------------------- resolution check
def _years(text: str) -> set[str]:
    ys = set(re.findall(r"\b(20\d\d)\b", text))
    for a, b in re.findall(r"\b(20\d\d)-(\d\d)\b", text):
        ys |= {a, a[:2] + b}
    return ys


def resolution_check(kind: str, pm: dict[str, Any], k: dict[str, Any]) -> dict[str, Any]:
    """Rules-based equivalence of the two resolution texts. Fail closed: missing text = caveat."""
    pr, kr = (pm.get("rules") or "").lower(), (k.get("rules") or "").lower()
    issues: list[str] = []
    hard: list[str] = []
    if not pr or not kr:
        issues.append("resolution text missing on one venue")
    py, ky = _years(pr + " " + pm.get("market_id", "")), _years(kr + " " + k.get("title", "").lower())
    if kind != "strike_touch" and py and ky and not py & ky:
        hard.append(f"season/year differs ({sorted(py)} vs {sorted(ky)})")
    pc, kc = es._ts(pm.get("closes_at")), es._ts(k.get("expected_expiration_time") or k.get("closes_at"))
    if kind == "strike_touch":
        if not ("any point" in pr or "at any time" in pr or "at any point" in pr) or "above" not in kr:
            hard.append("one side is not a 'touch' (high reached at any time) contract")
        if ("cf benchmarks" in pr or "brti" in pr) != ("cf bitcoin" in kr or "brti" in kr or "cf benchmarks" in kr):
            hard.append("different BTC price source")
        # listing close dates are padded on both venues; compare the deadline in the rule text
        if not ("dec 31, 2026" in kr or "december 31, 2026" in kr) or not any(
                d in pr for d in ("january 1, 2027", "december 31, 2026", "dec 31, 2026")):
            hard.append("deadline in the rule text differs")
        if "starting" in kr and "creation of this market" in pr:
            issues.append("touch window start differs (Kalshi fixed start, Polymarket from listing)")
    else:
        if pc and kc and abs((pc - kc).total_seconds()) > 400 * 86400:
            hard.append("resolution dates are far apart")
        if ("tie" in pr) != ("tie" in kr):
            issues.append("tie / dead-heat handling only described on one venue")
        if ("postpone" in pr or "cancel" in pr) != ("postpone" in kr or "cancel" in kr):
            issues.append("postponement/cancellation rules only described on one venue")
    verdict = "mismatch" if hard else "caveat" if issues else "equivalent"
    return {"verdict": verdict, "issues": hard + issues, "method": "rules"}


# ---------------------------------------------------------------------------- fetching
def _dollars(m: dict[str, Any], key: str) -> float | None:
    v = es._f(m.get(f"{key}_dollars"))
    if v is None and es._f(m.get(key)) is not None:
        v = es._f(m.get(key)) / 100
    return v


def kalshi_event(client: Any, series: str, event: str) -> tuple[list[dict[str, Any]], float]:
    out, cursor = [], None
    for _ in range(5):
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{KALSHI_API}/markets", params=params)
        r.raise_for_status()
        d = r.json()
        for m in d.get("markets") or []:
            if m.get("event_ticker") != event:
                continue
            q = {"venue": es.KALSHI, "market_id": m["ticker"], "name": m.get("yes_sub_title") or "",
                 "title": m.get("title") or "", "rules": (m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or ""),
                 "closes_at": m.get("close_time"), "expected_expiration_time": m.get("expected_expiration_time"),
                 "yes_bid": _dollars(m, "yes_bid"), "yes_ask": _dollars(m, "yes_ask"),
                 "no_bid": _dollars(m, "no_bid"), "no_ask": _dollars(m, "no_ask")}
            out.append(q)
        cursor = d.get("cursor")
        if not cursor:
            break
    return out, time.time()


def pmus_latest(conn: sqlite3.Connection, prefix: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT market_id, snapshot_json, MAX(captured_at) FROM market_snapshots WHERE venue=? AND market_id LIKE ? "
        "GROUP BY market_id", (es.PMUS, prefix + "%")).fetchall()
    out = []
    for mid, snap, _ in rows:
        try:
            s = json.loads(snap)
        except ValueError:
            continue
        out.append({"venue": es.PMUS, "market_id": mid, "name": str(s.get("title") or ""),
                    "title": str(s.get("title") or ""), "rules": str(s.get("resolution_rules") or ""),
                    "closes_at": s.get("closes_at"), **{k: es._f(s.get(k)) for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")}})
    return out


def pmus_bbo(pm_client: Any, slug: str) -> dict[str, float | None] | None:
    payload = None
    for attempt in range(3):
        try:
            payload = pm_client.markets.bbo(slug)
            break
        except Exception as exc:  # noqa: BLE001 - missing/limited quote stays unknown
            if type(exc).__name__ != "RateLimitError" or attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))
    d = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(d, dict) or d.get("marketSlug") != slug:
        return None

    def px(v: Any) -> float | None:
        if isinstance(v, dict):
            v = v.get("value")
        x = es._f(v)
        return x if x is not None and 0 <= x <= 1 else None
    bid, ask = px(d.get("bestBid")), px(d.get("bestAsk"))
    if bid is not None and ask is not None and bid > ask:
        return None
    return {"yes_bid": bid, "yes_ask": ask, "no_bid": None if ask is None else round(1 - ask, 4),
            "no_ask": None if bid is None else round(1 - bid, 4), "at": time.time()}


# ----------------------------------------------------------------------------- pairing
def match_family(kind: str, pms: list[dict[str, Any]], ks: list[dict[str, Any]]) -> tuple[list[tuple], int]:
    pairs, ambiguous = [], 0
    for pm in pms:
        if kind == "strike_touch":
            sp = strike_of(pm["name"])
            cands = [k for k in ks if sp is not None and strike_of(k["name"]) is not None
                     and abs(strike_of(k["name"]) - sp) < 0.011]
        else:
            cands = [k for k in ks if names_match(pm["name"], k["name"])]
        if len(cands) > 1:  # prefer the exact normalised name if one exists
            exact = [k for k in cands if norm_name(k["name"]) == norm_name(pm["name"])]
            cands = exact if len(exact) == 1 else cands
        if len(cands) != 1:
            ambiguous += len(cands) > 1
            continue
        k = cands[0]
        back = [p for p in pms if (names_match(p["name"], k["name"]) if kind != "strike_touch"
                                   else strike_of(p["name"]) == strike_of(pm["name"]))]
        if len(back) != 1:
            ambiguous += 1
            continue
        pairs.append((pm, k))
    return pairs, ambiguous


def gaps(pm: dict[str, Any], k: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a, b in ((pm, k), (k, pm)):
        ya, nb = a.get("yes_ask"), b.get("no_ask")
        if ya is None or nb is None or not 0 < ya < 1 or not 0 < nb < 1:
            continue
        cost = es.leg_cost(a["venue"], ya) + es.leg_cost(b["venue"], nb)
        out.append({"yes_venue": a["venue"], "yes_market": a["market_id"], "yes_ask": ya,
                    "no_venue": b["venue"], "no_market": b["market_id"], "no_ask": nb,
                    "raw_gap": round(1 - ya - nb, 4), "cost": round(cost, 4), "edge": round(1 - cost, 4)})
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS xv_pairs (
        pm_market TEXT, kalshi_market TEXT, family TEXT, name TEXT, verdict TEXT, issues_json TEXT,
        pm_json TEXT, k_json TEXT, best_json TEXT, skew_s REAL, at TEXT, PRIMARY KEY (pm_market, kalshi_market))""")
    conn.execute("CREATE TABLE IF NOT EXISTS xv_runs (at TEXT, families INTEGER, pairs INTEGER, ambiguous INTEGER,"
                 " equivalent INTEGER, positive INTEGER, note TEXT)")


def run(conn: sqlite3.Connection, *, client: Any = None, pm_client: Any = None,
        now: datetime | None = None, workers: int = 2) -> dict[str, Any]:
    """Match every family, refresh both books together, store pairs. Returns a summary."""
    import httpx

    now = now or datetime.now(UTC)
    ensure_schema(conn)
    own = client is None
    client = client or httpx.Client(timeout=10, headers={"accept": "application/json"})
    if pm_client is None:
        try:
            from polymarket_us import PolymarketUS
            pm_client = PolymarketUS(timeout=7.0, max_retries=0)
        except Exception:  # noqa: BLE001
            pm_client = None
    summary: dict[str, Any] = {"at": now.isoformat(), "families": {}, "pairs": 0, "ambiguous": 0,
                               "equivalent": 0, "positive": 0, "errors": []}
    rows = []
    try:
        for prefix, series, event, kind in FAMILIES:
            pms = pmus_latest(conn, prefix)
            conn.commit()
            try:
                ks, k_at = kalshi_event(client, series, event)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"{series}: {type(exc).__name__}")
                continue
            pairs, amb = match_family(kind, pms, ks)
            fresh = {}
            if pm_client is not None and pairs:
                with ThreadPoolExecutor(workers) as ex:
                    for (pm, _), bbo in zip(pairs, ex.map(lambda p: pmus_bbo(pm_client, p[0]["market_id"]), pairs)):
                        fresh[pm["market_id"]] = bbo
            fam = {"kind": kind, "pm_markets": len(pms), "kalshi_markets": len(ks), "pairs": len(pairs),
                   "ambiguous": amb, "positive": 0}
            for pm, k in pairs:
                bbo = fresh.get(pm["market_id"])
                skew = None
                if bbo:
                    pm = {**pm, **{x: bbo[x] for x in ("yes_bid", "yes_ask", "no_bid", "no_ask")}}
                    skew = round(abs(bbo["at"] - k_at), 1)
                chk = resolution_check(kind, pm, k)
                g = gaps(pm, k)
                best = max(g, key=lambda x: x["edge"]) if g else None
                synced = skew is not None and skew <= es.MAX_LEG_SKEW_S
                if best and best["edge"] > 0 and chk["verdict"] != "mismatch" and synced:
                    fam["positive"] += 1
                summary["equivalent"] += chk["verdict"] == "equivalent"
                rows.append((pm["market_id"], k["market_id"], event, k["name"] or pm["name"], chk["verdict"],
                             json.dumps(chk["issues"]),
                             json.dumps({x: pm.get(x) for x in ("title", "yes_bid", "yes_ask", "no_bid", "no_ask")}),
                             json.dumps({x: k.get(x) for x in ("title", "yes_bid", "yes_ask", "no_bid", "no_ask")}),
                             json.dumps(best), skew, now.isoformat()))
            summary["families"][event] = fam
            summary["pairs"] += len(pairs)
            summary["ambiguous"] += amb
            summary["positive"] += fam["positive"]
    finally:
        if own:
            client.close()
    conn.execute("DELETE FROM xv_pairs")
    conn.executemany("INSERT OR REPLACE INTO xv_pairs VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO xv_runs VALUES (?,?,?,?,?,?,?)",
                 (now.isoformat(), len(summary["families"]), summary["pairs"], summary["ambiguous"],
                  summary["equivalent"], summary["positive"], json.dumps(summary["errors"])))
    conn.commit()
    return summary


def stored_pairs(conn: sqlite3.Connection, max_age_min: float = 30) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT pm_market, kalshi_market, family, name, verdict, issues_json, pm_json, k_json, "
                            "best_json, skew_s, at FROM xv_pairs WHERE at >= datetime('now', ?)",
                            (f"-{int(max_age_min)} minutes",)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        out.append({"pm_market": r[0], "kalshi_market": r[1], "family": r[2], "name": r[3], "verdict": r[4],
                    "issues": json.loads(r[5] or "[]"), "pm": json.loads(r[6] or "{}"), "kalshi": json.loads(r[7] or "{}"),
                    "best": json.loads(r[8] or "null"), "skew_s": r[9], "at": r[10]})
    return out


def arb_signals(pairs: list[dict[str, Any]], *, min_edge: float = 0.02) -> list[dict[str, Any]]:
    """Paper pm_arb signals: only rules-equivalent, synchronously quoted pairs clear this gate."""
    out = []
    for p in pairs:
        b = p.get("best")
        if not b or p["verdict"] != "equivalent" or p.get("skew_s") is None or p["skew_s"] > es.MAX_LEG_SKEW_S:
            continue
        if b["edge"] < min_edge:
            continue
        out.append({"strategy": "pm_arb", "kind": "cross_venue_matched", "key": f"xvm:{b['yes_market']}|{b['no_market']}",
                    "title": f"{p['name']}: YES {b['yes_venue']} @{b['yes_ask']:.2f} + NO {b['no_venue']} @{b['no_ask']:.2f}",
                    "cost": b["cost"], "locked_payoff": 1.0, "edge": b["edge"], "needs_resolution_review": False,
                    "legs": [{"venue": b["yes_venue"], "market_id": b["yes_market"], "side": "yes", "price": b["yes_ask"]},
                             {"venue": b["no_venue"], "market_id": b["no_market"], "side": "no", "price": b["no_ask"]}]})
    return out


def main() -> None:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Kalshi <-> Polymarket US matcher (read-only)")
    ap.add_argument("--db", default="data/noema.db")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--every", type=float, default=600)
    a = ap.parse_args()
    while True:
        conn = sqlite3.connect(a.db, timeout=30)
        try:
            r = run(conn)
            print(json.dumps({k: r[k] for k in ("at", "pairs", "equivalent", "positive", "errors")}), flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, report honestly
            print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:200]}), flush=True)
        finally:
            conn.close()
        if not a.loop:
            return
        time.sleep(a.every)


if __name__ == "__main__":  # pragma: no cover
    main()
