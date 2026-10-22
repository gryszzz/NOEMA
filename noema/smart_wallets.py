"""Smart-wallet study + SMART-FOLLOW paper agent (PAPER / READ ONLY - nothing here signs or trades).

Data: pump.fun trades already captured by noema.launch_stream (launch_trades, early-life of every new coin, plus
every trade of *watched* wallets and 30 min of trades on coins they buy) + optional Helius backfill of a candidate
wallet's recent history (getSignaturesForAddress/getTransaction, throttled, key never logged).

Vetting (strictly prospective): a wallet is "vetted at time T" only from positions it CLOSED before T:
  n >= MIN_N closed positions, win rate >= 50%, realized PnL > 0 even after removing its single best trade,
  bootstrap 95% lower bound of mean PnL/position > 0, and it is not a dev (creator) / sniper (entries < 2 s after
  create) / bundler (shares a tx with other buyers) / bag-holder (>50% of older entries never closed).
SMART-FOLLOW: when a vetted wallet buys a coin <= 15 min old, paper-enter at the first trade >= 2 s later, exit on
TP +40% / SL -25% / the wallet selling / 20 min, net of 2%/side (fees + slippage). Outcomes -> edge_avenues.db
paper_outcomes(kind='smart_follow') so agent_lifecycle hires/fires it like every other roster agent.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

FEE = 0.0125          # pump.fun fee per side on the wallet's own trades
COST_SIDE = 0.02      # our paper cost per side (fee + slippage)
MIN_N = int(os.getenv("NOEMA_SMART_MIN_N", "8"))
SNIPE_S = 2.0
TP, SL, MAX_HOLD_S, MAX_AGE_S, LAT_S = 0.40, -0.25, 1200, 900, 2.0
MCAP_MIN, MCAP_MAX = 15.0, 500.0
NOTIONAL_USD = 5.0
WATCH_CAP = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_trades (signature TEXT, wallet TEXT, mint TEXT, at TEXT, side TEXT, sol REAL,
  tokens REAL, market_cap_sol REAL, PRIMARY KEY (signature, wallet, mint));
CREATE TABLE IF NOT EXISTS backfilled (wallet TEXT PRIMARY KEY, at TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS watch_wallets (wallet TEXT PRIMARY KEY, why TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS smart_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, report_json TEXT);
"""


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


# -- loading -------------------------------------------------------------------
def load_trades(main_db: str, smart_db: str | None = None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """All trades (time-ordered, deduped by signature+wallet) and mint meta {created_at, creator}."""
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    meta: dict[str, dict[str, Any]] = {}
    m = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=30)
    try:
        for mint, at, creator in m.execute("SELECT mint, received_at, creator FROM launch_stream"):
            meta[mint] = {"created_at": _ts(at), "creator": creator}
        for sig, mint, at, side, sol, tok, trader, mc in m.execute(
                "SELECT signature, mint, received_at, side, sol, tokens, trader, market_cap_sol FROM launch_trades "
                "WHERE trader IS NOT NULL"):
            rows[(sig, trader)] = {"sig": sig.split(":")[0], "wallet": trader, "mint": mint, "t": _ts(at), "side": side,
                                   "sol": float(sol or 0), "tokens": float(tok or 0), "mcap": mc}
    finally:
        m.close()
    if smart_db and os.path.isfile(smart_db):
        s = sqlite3.connect(f"file:{smart_db}?mode=ro", uri=True, timeout=30)
        try:
            for sig, w, mint, at, side, sol, tok, mc in s.execute("SELECT * FROM wallet_trades"):
                rows.setdefault((sig, w), {"sig": sig, "wallet": w, "mint": mint, "t": _ts(at), "side": side,
                                           "sol": float(sol or 0), "tokens": float(tok or 0), "mcap": mc})
        finally:
            s.close()
    return sorted(rows.values(), key=lambda r: r["t"]), meta


# -- positions -----------------------------------------------------------------
def build_positions(trades: list[dict[str, Any]], meta: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_sig: dict[str, set[str]] = defaultdict(set)
    for t in trades:
        if t["side"] == "buy":
            by_sig[t["sig"]].add(t["wallet"])
    pos: dict[tuple[str, str], dict[str, Any]] = {}
    for t in trades:
        k = (t["wallet"], t["mint"])
        p = pos.get(k)
        if p is None:
            if t["side"] != "buy":
                continue  # sells of a position opened before we saw it: unknown cost basis, skip
            mm = meta.get(t["mint"]) or {}
            age = t["t"] - mm["created_at"] if mm.get("created_at") else None
            p = pos[k] = {"wallet": t["wallet"], "mint": t["mint"], "entry_at": t["t"], "entry_mcap": t["mcap"],
                          "entry_age_s": age, "dev": bool(mm.get("creator")) and mm.get("creator") == t["wallet"],
                          "snipe": age is not None and age < SNIPE_S, "bundled": len(by_sig[t["sig"]]) > 1,
                          "cost": 0.0, "bought": 0.0, "proceeds": 0.0, "sold": 0.0, "n_sells": 0, "close_at": None}
        if p["close_at"] is not None:
            continue
        if t["side"] == "buy":
            p["cost"] += t["sol"] * (1 + FEE)
            p["bought"] += t["tokens"]
            p["first_size_sol"] = p.get("first_size_sol", t["sol"])
        else:
            p["proceeds"] += t["sol"] * (1 - FEE)
            p["sold"] += t["tokens"]
            p["n_sells"] += 1
            if p["bought"] > 0 and p["sold"] >= 0.95 * p["bought"]:
                p["close_at"] = t["t"]
                p["pnl_sol"] = p["proceeds"] - p["cost"]
                p["ret"] = p["pnl_sol"] / p["cost"] if p["cost"] else 0.0
                p["hold_s"] = t["t"] - p["entry_at"]
                p["partial"] = p["n_sells"] > 1
    return list(pos.values())


def bootstrap_lo(xs: list[float], n: int = 1000, seed: int = 7) -> float:
    rng = random.Random(seed)
    k = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(k)) / k for _ in range(n))
    return means[int(0.025 * n)]


def wallet_stats(positions: list[dict[str, Any]], asof: float) -> dict[str, dict[str, Any]]:
    """Per-wallet stats using ONLY information available at `asof` (closes before asof, entries before asof)."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in positions:
        if p["entry_at"] < asof:
            by[p["wallet"]].append(p)
    out = {}
    for w, ps in by.items():
        closed = [p for p in ps if p["close_at"] is not None and p["close_at"] < asof]
        old = [p for p in ps if p["entry_at"] < asof - 7200]
        old_open = [p for p in old if p["close_at"] is None or p["close_at"] >= asof]
        pnl = [p["pnl_sol"] for p in closed]
        st = {"n_entries": len(ps), "n_closed": len(closed), "win_rate": sum(x > 0 for x in pnl) / len(pnl) if pnl else None,
              "pnl_sol": round(sum(pnl), 4), "pnl_ex_best": round(sum(pnl) - max(pnl), 4) if pnl else None,
              "dev_share": sum(p["dev"] for p in ps) / len(ps), "snipe_share": sum(p["snipe"] for p in ps) / len(ps),
              "bundle_share": sum(p["bundled"] for p in ps) / len(ps),
              "open_ratio": len(old_open) / len(old) if old else 0.0, "ci_lo": None}
        if len(pnl) >= MIN_N:
            st["ci_lo"] = round(bootstrap_lo(pnl), 5)
        st["vetted"], st["why"] = _vet(st)
        out[w] = st
    return out


def _vet(st: dict[str, Any]) -> tuple[bool, str]:
    if st["n_closed"] < MIN_N:
        return False, f"n_closed {st['n_closed']} < {MIN_N}"
    if st["dev_share"] > 0:
        return False, "dev/creator wallet"
    if st["snipe_share"] > 0.5:
        return False, "sniper (most entries < 2s after create)"
    if st["bundle_share"] > 0.3:
        return False, "bundler (shares buy txs)"
    if st["open_ratio"] > 0.5:
        return False, "bag-holder (most older entries never closed)"
    if (st["win_rate"] or 0) < 0.5:
        return False, "win rate < 50%"
    if (st["pnl_ex_best"] or 0) <= 0:
        return False, "one lucky hit (PnL <= 0 without best trade)"
    if (st["ci_lo"] or 0) <= 0:
        return False, "bootstrap 95% lower bound <= 0"
    return True, "vetted"


def patterns(positions: list[dict[str, Any]], wallets: set[str]) -> dict[str, Any]:
    ps = [p for p in positions if p["wallet"] in wallets and p["close_at"] is not None]

    def q(xs: list[float]) -> dict[str, Any]:
        xs = sorted(x for x in xs if x is not None)
        return {"n": len(xs), "p25": xs[len(xs) // 4] if xs else None, "p50": xs[len(xs) // 2] if xs else None,
                "p75": xs[3 * len(xs) // 4] if xs else None}
    return {"entry_age_s": q([p["entry_age_s"] for p in ps]), "entry_mcap_sol": q([p["entry_mcap"] for p in ps]),
            "size_sol": q([p.get("first_size_sol") for p in ps]), "hold_s": q([p["hold_s"] for p in ps]),
            "partial_sell_share": round(sum(p["partial"] for p in ps) / len(ps), 3) if ps else None,
            "win_ret": q([p["ret"] for p in ps if p["ret"] > 0]), "loss_ret": q([p["ret"] for p in ps if p["ret"] <= 0])}


# -- SMART-FOLLOW paper eval (no look-ahead) --------------------------------------
def follow_signals(trades: list[dict[str, Any]], positions: list[dict[str, Any]], meta: dict[str, dict[str, Any]],
                   now: float) -> list[dict[str, Any]]:
    by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if t.get("mcap"):
            by_mint[t["mint"]].append(t)
    vet_cache: dict[int, dict[str, dict[str, Any]]] = {}
    out, seen = [], set()
    for t in trades:
        if t["side"] != "buy" or t["mint"] in seen:
            continue
        mm = meta.get(t["mint"]) or {}
        if not mm.get("created_at") or t["t"] - mm["created_at"] > MAX_AGE_S or mm.get("creator") == t["wallet"]:
            continue
        hour = int(t["t"] // 3600)  # vetting snapshot at the hour boundary BEFORE the signal
        if hour not in vet_cache:
            vet_cache[hour] = wallet_stats(positions, hour * 3600.0)
        st = vet_cache[hour].get(t["wallet"])
        if not st or not st["vetted"]:
            continue
        seen.add(t["mint"])
        later = [x for x in by_mint[t["mint"]] if x["t"] >= t["t"] + LAT_S]
        if not later:
            continue
        e = later[0]
        if not (MCAP_MIN <= e["mcap"] <= MCAP_MAX):
            continue
        exit_px, why, exit_t = None, None, None
        for x in later[1:]:
            if x["t"] - e["t"] > MAX_HOLD_S:
                break
            r = x["mcap"] / e["mcap"] - 1
            if r >= TP or r <= SL or (x["wallet"] == t["wallet"] and x["side"] == "sell"):
                exit_px, exit_t = x["mcap"], x["t"]
                why = "tp" if r >= TP else "sl" if r <= SL else "wallet_sold"
                break
        if exit_px is None:
            if now - e["t"] < MAX_HOLD_S:
                continue  # still open -> not scored yet
            last = [x for x in later if x["t"] - e["t"] <= MAX_HOLD_S][-1]
            exit_px, exit_t, why = last["mcap"], last["t"], "time"
        ret = exit_px / e["mcap"] - 1 - 2 * COST_SIDE
        out.append({"id": f"sf:{t['mint']}", "at": _iso(e["t"]), "wallet": t["wallet"], "mint": t["mint"],
                    "entry_mcap": e["mcap"], "exit_mcap": exit_px, "exit": why, "hold_s": round(exit_t - e["t"], 1),
                    "ret": round(ret, 4), "net": round(NOTIONAL_USD * ret, 4),
                    "x": {"entry_age_s": round(t["t"] - mm["created_at"], 1), "entry_mcap": e["mcap"],
                          "wallet_ci_lo": st["ci_lo"], "wallet_n": st["n_closed"]}})
    return out


def smart_features(positions: list[dict[str, Any]], mint: str, asof: float,
                   vetted: dict[str, dict[str, Any]] | None = None) -> dict[str, float]:
    """Features for meta-launch / timing agents: vetted (as of the previous hour) wallets that bought `mint` before asof."""
    vetted = vetted if vetted is not None else wallet_stats(positions, (asof // 3600) * 3600)
    buys = [p for p in positions if p["mint"] == mint and p["entry_at"] < asof and (vetted.get(p["wallet"]) or {}).get("vetted")]
    return {"smart_buyers": float(len(buys)), "smart_buy_sol": round(sum(p.get("first_size_sol") or 0 for p in buys), 4)}


# -- Helius backfill (free tier: <= ~3 req/s, capped per cycle) ----------------------
def _rpc(method: str, params: list[Any]) -> Any:
    import urllib.request
    key = os.getenv("HELIUS_API_KEY")
    if not key:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(f"https://mainnet.helius-rpc.com/?api-key={key}", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("result")


def parse_tx_trades(tx: dict[str, Any], wallet: str, sig: str) -> list[dict[str, Any]]:
    import base64

    from .launch_stream import decode_event
    out = []
    for ln in ((tx.get("meta") or {}).get("logMessages") or []):
        if not ln.startswith("Program data: "):
            continue
        try:
            ev = decode_event(base64.b64decode(ln[14:]))
        except ValueError:
            continue
        if ev and ev["kind"] == "trade" and ev["trader"] == wallet:
            out.append({"sig": sig, "wallet": wallet, "mint": ev["mint"], "t": float(tx.get("blockTime") or 0),
                        "side": ev["side"], "sol": ev["sol"], "tokens": ev["tokens"], "mcap": ev["market_cap_sol"]})
    return out


def backfill(c: sqlite3.Connection, wallets: list[str], max_tx: int = 400, per_wallet: int = 60) -> int:
    n_calls, n = 0, 0
    for w in wallets:
        if n_calls >= max_tx:
            break
        try:
            sigs = _rpc("getSignaturesForAddress", [w, {"limit": per_wallet}]) or []
            n_calls += 1
            got = 0
            for s in sigs:
                if s.get("err") is not None or n_calls >= max_tx:
                    continue
                time.sleep(0.35)
                tx = _rpc("getTransaction", [s["signature"], {"maxSupportedTransactionVersion": 0}])
                n_calls += 1
                for t in parse_tx_trades(tx or {}, w, s["signature"]):
                    c.execute("INSERT OR IGNORE INTO wallet_trades VALUES (?,?,?,?,?,?,?,?)",
                              (t["sig"], w, t["mint"], _iso(t["t"]), t["side"], t["sol"], t["tokens"], t["mcap"]))
                    got += 1
            c.execute("INSERT OR REPLACE INTO backfilled VALUES (?,?,?)", (w, datetime.now(UTC).isoformat(), got))
            c.commit()
            n += got
        except Exception as exc:  # noqa: BLE001 - never print the URL (holds the key)
            print(datetime.now(UTC).isoformat(), "backfill", type(exc).__name__, flush=True)
            time.sleep(5)
    return n


def candidates(positions: list[dict[str, Any]], k: int = WATCH_CAP) -> list[str]:
    cnt: dict[str, set[str]] = defaultdict(set)
    bad: set[str] = set()
    for p in positions:
        cnt[p["wallet"]].add(p["mint"])
        if p["dev"]:
            bad.add(p["wallet"])
    ranked = sorted((w for w in cnt if w not in bad and len(cnt[w]) >= 5), key=lambda w: -len(cnt[w]))
    return ranked[:k]


# -- cycle -----------------------------------------------------------------------
def cycle(main_db: str, smart_db: str, avenues_db: str | None, *, do_backfill: bool = True) -> dict[str, Any]:
    now = time.time()
    c = connect(smart_db)
    try:
        trades, meta = load_trades(main_db, smart_db)
        pos = build_positions(trades, meta)
        st = wallet_stats(pos, now)
        vetted = {w for w, s in st.items() if s["vetted"]}
        cand = candidates(pos)
        if do_backfill and os.getenv("HELIUS_API_KEY"):
            day = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            done = {r[0] for r in c.execute("SELECT wallet FROM backfilled WHERE at>=?", (day,))}
            todo = [w for w in cand if w not in done][:8]
            if todo:
                backfill(c, todo)
                trades, meta = load_trades(main_db, smart_db)
                pos = build_positions(trades, meta)
                st = wallet_stats(pos, now)
                vetted = {w for w, s in st.items() if s["vetted"]}
        watch = list(vetted) + [w for w in cand if w not in vetted]
        c.execute("DELETE FROM watch_wallets")
        c.executemany("INSERT INTO watch_wallets VALUES (?,?,?)",
                      [(w, "vetted" if w in vetted else "candidate", _iso(now)) for w in watch[:WATCH_CAP]])
        sig = follow_signals(trades, pos, meta, now)
        if avenues_db:
            a = sqlite3.connect(avenues_db, timeout=30)
            a.execute("CREATE TABLE IF NOT EXISTS paper_outcomes (kind TEXT, id TEXT, at TEXT, net REAL, x_json TEXT, "
                      "PRIMARY KEY (kind, id))")
            a.executemany("INSERT OR IGNORE INTO paper_outcomes VALUES (?,?,?,?,?)",
                          [("smart_follow", s["id"], s["at"], s["net"], json.dumps(s["x"])) for s in sig])
            a.commit()
            a.close()
        rets = [s["net"] for s in sig]
        closed_all = [p for p in pos if p["close_at"] is not None]
        report = {"at": _iso(now), "mode": "paper/read-only", "trades": len(trades), "wallets": len(st),
                  "positions": len(pos), "closed_positions": len(closed_all), "candidates": len(cand),
                  "vetted": sorted(({"wallet": w, **st[w]} for w in vetted), key=lambda r: -(r["ci_lo"] or 0))[:25],
                  "rejections": _rej_counts(st), "patterns_vetted": patterns(pos, vetted),
                  "patterns_all_closed": patterns(pos, set(st)),
                  "smart_follow": {"n": len(sig), "mean_net_usd": round(sum(rets) / len(rets), 4) if rets else None,
                                   "ci95_lo_usd": round(bootstrap_lo(rets), 4) if len(rets) >= 5 else None,
                                   "win_rate": round(sum(r > 0 for r in rets) / len(rets), 3) if rets else None,
                                   "recent": sig[-10:]},
                  "recent_moves": _recent_moves(trades, vetted | set(cand[:50]), now, vetted)}
        c.execute("INSERT INTO smart_reports (at, report_json) VALUES (?,?)", (report["at"], json.dumps(report)))
        c.execute("DELETE FROM smart_reports WHERE id NOT IN (SELECT id FROM smart_reports ORDER BY id DESC LIMIT 48)")
        c.commit()
        return report
    finally:
        c.close()


def _rej_counts(st: dict[str, dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for s in st.values():
        out[s["why"].split(" (")[0] if not s["why"].startswith("n_closed") else "too few closed positions"] += 1
    return dict(out)


def _recent_moves(trades: list[dict[str, Any]], wallets: set[str], now: float, vetted: set[str]) -> list[dict[str, Any]]:
    out = [{"at": _iso(t["t"]), "wallet": t["wallet"], "vetted": t["wallet"] in vetted, "mint": t["mint"],
            "side": t["side"], "sol": round(t["sol"], 4), "mcap_sol": t["mcap"]}
           for t in trades if t["wallet"] in wallets and t["t"] >= now - 1800]
    return out[-40:][::-1]


def latest_report(smart_db: str) -> dict[str, Any] | None:
    if not os.path.isfile(smart_db):
        return None
    c = sqlite3.connect(f"file:{smart_db}?mode=ro", uri=True, timeout=10)
    try:
        r = c.execute("SELECT report_json FROM smart_reports ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(r[0]) if r else None
    except sqlite3.Error:
        return None
    finally:
        c.close()


def load_watch(smart_db: str) -> dict[str, str]:
    if not os.path.isfile(smart_db):
        return {}
    try:
        c = sqlite3.connect(f"file:{smart_db}?mode=ro", uri=True, timeout=10)
        try:
            return dict(c.execute("SELECT wallet, why FROM watch_wallets"))
        finally:
            c.close()
    except sqlite3.Error:
        return {}


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--smart-db", default="data/smart_wallets.db")
    ap.add_argument("--avenues-db", default="data/edge_avenues.db")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--no-backfill", action="store_true")
    a = ap.parse_args()
    while True:
        try:
            r = cycle(a.db, a.smart_db, a.avenues_db, do_backfill=not a.no_backfill)
            print(r["at"], json.dumps({k: r[k] for k in ("trades", "wallets", "closed_positions", "candidates")}),
                  "vetted", len(r["vetted"]), "smart_follow", json.dumps({k: v for k, v in r["smart_follow"].items()
                                                                          if k != "recent"}), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(datetime.now(UTC).isoformat(), "smart_wallets", type(exc).__name__, str(exc)[:160], flush=True)
        if not a.loop:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
