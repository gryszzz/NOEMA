"""META LAUNCH agent (PAPER ONLY): buy fresh launches riding a crypto-Twitter / launchpad meta.

1) Meta detection: term streams from X posts, culture radar and pump.fun/DEX launch names are counted in
   rolling 30-min windows; velocity (now), acceleration (now - prev), stage (early/peak/fading) and the
   meta's past hit rate (share of its earlier matched launches that reached +50% within 1h).
2) Launch matching: launches in the last 15 min whose name/ticker share a hot meta term; an X mention of
   the contract address boosts the match. The first credible launch per meta is ``first``; later ones are
   ``copycat``.
3) Staged-pump shield: mint/freeze authority, serial dev launches, dev holding, top-holder concentration,
   tiny holder count with volume (bundle/sniper proxy), shill-shield warnings, KOL-wallet dumps, and an
   organic-score wash proxy. Wallet-level funding clusters are NOT available in our data (stated in /api/metas).
4) Entry/exit: paper probe size until a meta agent is proven in the roster; adaptive exits with a fast TP,
   a trailing runner, a hard stop and a short time stop that only extends while the meta is not fading.
5) Learn: every meta snapshot and matched launch is logged with features; outcomes come from the trench
   observation path (30s..6h); walk-forward + Benjamini-Hochberg in :func:`walk_forward`.
Nothing here can place a real order.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from . import adaptive_exits as ax
from . import edge_mining as em
from .sqlite_util import connect as sqlite_connect

WINDOW_MIN = 30
BASELINE = timedelta(hours=6)
SURGE = 2.5
MATCH_WINDOW_MIN = 15
STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "it", "you", "they", "we", "i", "my", "me", "our", "your", "this", "that", "with", "at", "by", "from", "be", "are", "was", "were", "as", "not", "no", "yes", "coin", "token", "inu", "pump", "sol", "solana", "ca", "new", "just", "get", "got", "all", "can", "will", "do", "so", "up", "out", "one", "go", "its", "it's", "official", "real", "first", "only", "now", "x", "ai's", "the1", "yo", "hello", "tweeted", "claimed"}
STOP -= {"ai"}
TOK = re.compile(r"[a-z][a-z0-9]{1,24}")
PROBE_USD = float(os.getenv("NOEMA_META_PROBE_USD", "2.5"))
BANKROLL = float(os.getenv("NOEMA_META_PAPER_BANKROLL_USD", "1000"))
META_EXIT = {"tp1": 0.5, "tp1_sell": 0.6, "runner": 0.25, "trail_atr": 2.0, "stop": -0.25,
             "time_stop_h": 0.5, "extend_h": 0.5, "max_hold_h": 6.0, "liq_pull": 0.35, "vol_dry": 0.15}
FEE = 0.02
CAP = 3.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS metas (meta TEXT, at TEXT, terms TEXT, stage TEXT, x_now INTEGER, launches_now INTEGER,
  culture_now INTEGER, velocity REAL, accel REAL, hit_rate REAL, score REAL, PRIMARY KEY (meta, at));
CREATE TABLE IF NOT EXISTS meta_matches (mint TEXT PRIMARY KEY, meta TEXT, matched_at TEXT, launched_at TEXT,
  name TEXT, symbol TEXT, rank TEXT, sim REAL, x_ca_mentions INTEGER, shield_ok INTEGER, shield_json TEXT,
  features_json TEXT, outcome_json TEXT);
CREATE TABLE IF NOT EXISTS meta_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT UNIQUE, meta TEXT,
  opened_at TEXT, entry REAL, size_usd REAL, tier TEXT, state_json TEXT, status TEXT, closed_at TEXT,
  reason TEXT, realized_ret REAL);
CREATE TABLE IF NOT EXISTS meta_runs (at TEXT, note TEXT);
"""


def tokens(text: str | None) -> set[str]:
    return {t for t in TOK.findall((text or "").lower().replace("$", " ")) if t not in STOP and len(t) >= 3}


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _ro(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite_connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ------------------------------------------------------------------ data
def load_launches(c: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    out = []
    for r in c.execute("SELECT mint, first_pool_at, discovery_json FROM trench_launches WHERE first_pool_at>=? "
                       "ORDER BY first_pool_at", (since,)):
        try:
            d = json.loads(r["discovery_json"])
        except ValueError:
            continue
        at = _ts(r["first_pool_at"])
        if not at:
            continue
        name, sym = d.get("name") or "", d.get("symbol") or ""
        out.append({"mint": r["mint"], "at": at, "name": name, "symbol": sym, "terms": tokens(f"{name} {sym}"),
                    "d": d})
    # live stream (noema.launch_stream): creates the poller has not seen yet count toward meta velocity at once
    seen = {x["mint"] for x in out}
    try:
        rows = c.execute("SELECT mint, received_at, name, symbol FROM launch_stream WHERE received_at>=? "
                         "ORDER BY received_at", (since,)).fetchall()
    except sqlite3.Error:
        rows = []
    for mint, rat, name, sym in rows:
        at = _ts(rat)
        if mint in seen or not at:
            continue
        out.append({"mint": mint, "at": at, "name": name or "", "symbol": sym or "",
                    "terms": tokens(f"{name or ''} {sym or ''}"),
                    "d": {"name": name, "symbol": sym, "launchpad": "pump.fun", "stream_only": True}})
    out.sort(key=lambda x: x["at"])
    return out


def load_x(c: sqlite3.Connection, since: str) -> list[tuple[datetime, set[str]]]:
    try:
        rows = c.execute("SELECT created_at, text FROM x_posts WHERE created_at>=?", (since,)).fetchall()
    except sqlite3.Error:
        return []
    return sorted(((t, tokens(r["text"])) for r in rows if (t := _ts(r["created_at"]))), key=lambda x: x[0])


def load_culture(c: sqlite3.Connection, since: str) -> list[tuple[datetime, set[str]]]:
    try:
        rows = c.execute("SELECT first_seen, title, symbol FROM culture_signals WHERE first_seen>=? "
                         "AND kind IN ('token','ticker')", (since,)).fetchall()
    except sqlite3.Error:
        return []
    return sorted(((t, tokens(f"{r['title']} {r['symbol']}")) for r in rows if (t := _ts(r["first_seen"]))),
                  key=lambda x: x[0])


def x_ca_mentions(c: sqlite3.Connection) -> dict[str, tuple[int, int, float]]:
    try:
        return {r["address"]: (r["mentions"] or 0, r["kol_authors"] or 0, r["shill_score"] or 0.0)
                for r in c.execute("SELECT address, mentions, kol_authors, shill_score FROM x_contracts")}
    except sqlite3.Error:
        return {}


def shill_warnings(c: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    try:
        for r in c.execute("SELECT entity, reasons FROM x_shill_warnings"):
            addr = (r["entity"] or "").split(":")[-1]
            try:
                out[addr] = json.loads(r["reasons"] or "[]")
            except ValueError:
                out[addr] = [str(r["reasons"])]
    except sqlite3.Error:
        pass
    return out


# ------------------------------------------------------------------ metas
class TermClock:
    """Rolling per-term counts for three consecutive windows, per source."""

    def __init__(self, window: timedelta) -> None:
        self.w = window
        self.events: deque[tuple[datetime, str, frozenset[str]]] = deque()
        self.hist: dict[str, list[tuple[datetime, int]]] = defaultdict(list)

    def add(self, at: datetime, src: str, terms: set[str]) -> None:
        self.events.append((at, src, frozenset(terms)))

    def snapshot(self, now: datetime) -> dict[str, dict[str, Any]]:
        while self.events and self.events[0][0] < now - BASELINE:
            self.events.popleft()
        win = [Counter(), Counter(), Counter()]
        base = Counter()
        src_now: dict[str, Counter] = defaultdict(Counter)
        co: dict[str, Counter] = defaultdict(Counter)
        for at, src, terms in self.events:
            if at > now:
                continue
            k = int((now - at) / self.w)
            for t in terms:
                base[t] += 1
            if k > 2:
                continue
            for t in terms:
                win[k][t] += 1
                if k == 0:
                    src_now[src][t] += 1
                    for u in terms:
                        if u != t:
                            co[t][u] += 1
        out = {}
        for t, n in win[0].items():
            prev, prev2 = win[1][t], win[2][t]
            launches, xs = src_now["launch"][t], src_now["x"][t]
            if n < 3 or (launches < 2 and xs < 2):
                continue
            per_win = base[t] / max(1.0, BASELINE / self.w)
            if n < SURGE * per_win:      # always-common words (cat, dog, inu...) are not a meta
                continue
            peak = max(n, prev, prev2)
            stage = "early" if n > prev and prev <= prev2 + 1 else "peak" if n >= 0.8 * peak else "fading"
            if n < 0.5 * peak:
                stage = "fading"
            related = [u for u, k in co[t].most_common(4) if k >= max(2, 0.4 * n)]
            out[t] = {"surge": round(n / max(per_win, 0.5), 2), "meta": t, "terms": [t] + related, "now": n, "prev": prev, "velocity": n / (self.w.seconds / 60),
                      "accel": n - prev, "stage": stage, "launches_now": launches, "x_now": xs,
                      "culture_now": src_now["culture"][t],
                      "cross": bool(launches and (xs or src_now["culture"][t]))}
        return out


def meta_score(m: dict[str, Any], hit_rate: float | None) -> float:
    s = min(1.0, m["now"] / 20) * 0.4 + (0.25 if m["cross"] else 0) + min(0.2, max(0, m["accel"]) / 25)
    s += {"early": 0.15, "peak": 0.05, "fading": -0.2}[m["stage"]]
    if hit_rate is not None:
        s += 0.2 * (hit_rate - 0.2)
    return round(max(0.0, min(1.0, s)), 4)


# ------------------------------------------------------------------ shield
_SMART: dict[str, Any] = {}
SMART_TTL_S = 600


def _vetted_wallets() -> frozenset[str]:
    """Vetted wallets from the smart_wallets loop's last cycle (watch_wallets, why='vetted'), cached 10 min.

    Previously this rebuilt every position from the full trade history in-process (~2.2 GB RSS); the smart_wallets
    loop already publishes the vetted set, so only that small set is cached here."""
    now = time.time()
    if now - _SMART.get("at", 0) > SMART_TTL_S:
        from .smart_wallets import load_watch
        w = load_watch(os.getenv("NOEMA_SMART_DB", "data/smart_wallets.db"))
        _SMART.update(at=now, vet=frozenset(k for k, why in w.items() if why == "vetted"))
    return _SMART["vet"]


def _smart_feats(mint: str, c: sqlite3.Connection | None = None, asof: datetime | None = None) -> dict[str, float]:
    """Vetted smart-wallet buys of this mint before `asof` (first buy per wallet). Zeros if unavailable.

    Per-mint indexed query on launch_trades (mint, received_at) - never loads the full trade table."""
    try:
        vet = _vetted_wallets()
        if not vet:
            return {"smart_buyers": 0.0, "smart_buy_sol": 0.0}
        own = c is None
        if own:
            c = _ro(os.getenv("NOEMA_DB_PATH", "data/noema.db"))
        try:
            until = (asof or datetime.now(UTC)).isoformat()
            first: dict[str, float] = {}
            for w, sol in c.execute("SELECT trader, sol FROM launch_trades WHERE mint=? AND received_at<? "
                                    "AND side='buy' AND trader IS NOT NULL ORDER BY received_at", (mint, until)):
                if w in vet and w not in first:
                    first[w] = float(sol or 0)
        finally:
            if own:
                c.close()
        return {"smart_buyers": float(len(first)), "smart_buy_sol": round(sum(first.values()), 4)}
    except Exception:  # noqa: BLE001
        return {"smart_buyers": 0.0, "smart_buy_sol": 0.0}


def retired() -> bool:
    """META strategy retired (default): keep scoring metas/matches for the record, but open NO new paper positions.
    Existing open positions are still managed to exit. Set NOEMA_META_RETIRED=0 to re-enable entries."""
    return os.getenv("NOEMA_META_RETIRED", "1").strip().lower() not in {"0", "false", "no", "off"}


def shield(launch: dict[str, Any], ca: dict[str, tuple[int, int, float]], warn: dict[str, list[str]]
           ) -> tuple[bool, list[str], float]:
    d = launch["d"]
    a = d.get("audit") or {}
    s1 = d.get("stats5m") or d.get("stats1h") or {}
    bad: list[str] = []
    soft = 0.0
    if d.get("stream_only"):
        bad.append("stream-only: no audit/holder data yet")  # fail closed until the poller enriches it
    if a.get("mintAuthorityDisabled") is False or a.get("freezeAuthorityDisabled") is False:
        bad.append("mint/freeze authority live")
    dev_mints = a.get("devMints") or 0
    if dev_mints >= 20:
        bad.append(f"serial launcher dev ({dev_mints} mints)")
    elif dev_mints >= 5:
        soft += 0.25
    if (a.get("devBalancePercentage") or 0) > 10:
        bad.append(f"dev holds {a['devBalancePercentage']:.0f}%")
    if (a.get("topHoldersPercentage") or 0) > 35:
        bad.append(f"top holders {a['topHoldersPercentage']:.0f}%")
    holders, traders, buys = d.get("holderCount") or 0, s1.get("numTraders") or 0, s1.get("numBuys") or 0
    if buys >= 20 and holders and holders <= 5:
        bad.append("bundle/sniper pattern: many buys, few holders")
    vol = (s1.get("buyVolume") or 0) + (s1.get("sellVolume") or 0)
    if vol > 5000 and (d.get("organicScore") or 0) < 5 and traders < 10:
        bad.append("wash-volume proxy: big volume, few traders, organic≈0")
    w = warn.get(launch["mint"])
    if w:
        bad.append("shill shield: " + ",".join(w)[:60])
    m, kols, shill = ca.get(launch["mint"], (0, 0, 0.0))
    if shill >= 0.6:
        bad.append(f"coordinated KOL shill score {shill:.2f}")
    elif kols >= 3 and m <= kols + 1:
        soft += 0.3
    return (not bad, bad, soft)


# ------------------------------------------------------------------ outcomes
def outcome_paths(c: sqlite3.Connection, mints: list[str]) -> dict[str, list[dict[str, float]]]:
    out: dict[str, list[dict[str, float]]] = defaultdict(list)
    for i in range(0, len(mints), 500):
        chunk = mints[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in c.execute(f"SELECT mint, horizon_seconds, tick_json FROM trench_observations WHERE mint IN ({q}) "
                           "ORDER BY mint, horizon_seconds", chunk):
            try:
                t = json.loads(r["tick_json"])
                p = float(t.get("price_usd") or 0)
            except (TypeError, ValueError):
                continue
            if p > 0:
                out[r["mint"]].append({"h": r["horizon_seconds"] / 3600, "price": p,
                                       "liq": t.get("liquidity_usd") or 0,
                                       "vol": (t.get("buy_volume_usd") or 0) + (t.get("sell_volume_usd") or 0)})
    return out


def trade_return(path: list[dict[str, float]], exit_params: dict[str, Any] | None = None) -> float | None:
    """Enter at the first observation (~30s), adaptive meta exits, fee + slippage on a probe clip."""
    if len(path) < 3:
        return None
    p = ax.AdaptiveExit.from_genome({**META_EXIT, **(exit_params or {})})
    slip = min(1.0, 2 * 25.0 / max(path[0]["liq"] or 1.0, 1.0))
    return min(CAP, ax.new_rules_return(path, FEE, p)) - slip


# ------------------------------------------------------------------ replay (used live and for backtests)
def replay(c: sqlite3.Connection, since: datetime, until: datetime, *, step_min: int = 5
           ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Walk time forward; at each step use ONLY data up to that step. Returns (matches, metas by step)."""
    s_iso = (since - BASELINE).isoformat()
    launches = load_launches(c, s_iso)
    ev: list[tuple[datetime, str, set[str]]] = [(l["at"], "launch", l["terms"]) for l in launches]
    ev += [(t, "x", s) for t, s in load_x(c, s_iso)] + [(t, "culture", s) for t, s in load_culture(c, s_iso)]
    ev.sort(key=lambda e: e[0])
    ca, warn = x_ca_mentions(c), shill_warnings(c)
    clock = TermClock(timedelta(minutes=WINDOW_MIN))
    i = 0
    li = 0
    seen_meta_first: dict[str, str] = {}
    meta_hits: dict[str, list[int]] = defaultdict(list)
    matches: list[dict[str, Any]] = []
    metas_by_step: dict[str, list[dict[str, Any]]] = {}
    now = since
    pending: list[dict[str, Any]] = []
    while now <= until:
        while i < len(ev) and ev[i][0] <= now:
            clock.add(*ev[i])
            i += 1
        metas = clock.snapshot(now)
        for m in metas.values():
            hist = meta_hits.get(m["meta"])
            m["hit_rate"] = round(sum(hist) / len(hist), 3) if hist and len(hist) >= 5 else None
            m["score"] = meta_score(m, m["hit_rate"])
        hot = {k: v for k, v in metas.items() if v["score"] >= 0.35 and v["stage"] != "fading"}
        metas_by_step[now.isoformat()] = sorted(metas.values(), key=lambda m: -m["score"])[:12]
        while li < len(launches) and launches[li]["at"] <= now:
            la = launches[li]
            li += 1
            if la["at"] < now - timedelta(minutes=MATCH_WINDOW_MIN):
                continue
            best = None
            for t in la["terms"] & set(hot):
                m = hot[t]
                sim = len(la["terms"] & set(m["terms"])) / max(1, len(la["terms"]))
                key = (m["score"] + 0.3 * sim, t)
                if best is None or key > best[0]:
                    best = (key, m, sim)
            if not best:
                continue
            _, m, sim = best
            ok, why, soft = shield(la, ca, warn)
            cam = ca.get(la["mint"], (0, 0, 0.0))[0]
            rank = "first" if ok and m["meta"] not in seen_meta_first else "copycat"
            if ok and m["meta"] not in seen_meta_first:
                seen_meta_first[m["meta"]] = la["mint"]
            a = la["d"].get("audit") or {}
            feats = {"meta_score": m["score"], "meta_now": m["now"], "meta_accel": m["accel"],
                     "stage_early": 1.0 if m["stage"] == "early" else 0.0, "cross": 1.0 if m["cross"] else 0.0,
                     "sim": round(sim, 3), "first": 1.0 if rank == "first" else 0.0, "x_ca": float(cam),
                     "soft_risk": soft, "dev_mints": float(a.get("devMints") or 0),
                     "top_holders": float(a.get("topHoldersPercentage") or 0),
                     "liquidity_usd": float(la["d"].get("liquidity") or 0),
                     "hit_rate": m["hit_rate"] if m["hit_rate"] is not None else -1.0}
            feats.update(_smart_feats(la["mint"], c, now))
            rec = {"mint": la["mint"], "meta": m["meta"], "matched_at": now, "launched_at": la["at"],
                   "name": la["name"], "symbol": la["symbol"], "rank": rank, "sim": sim, "x_ca": cam,
                   "shield_ok": ok, "shield": why, "features": feats, "stage": m["stage"]}
            matches.append(rec)
            pending.append(rec)
        # meta hit-rate learning: only from matches whose 1h outcome is already in the past
        still = []
        for rec in pending:
            if rec["launched_at"] + timedelta(hours=1) <= now:
                rec.setdefault("_due", True)
            else:
                still.append(rec)
        pending = still
        now += timedelta(minutes=step_min)
    # outcomes (known after the fact; used for scoring/hit rates, never for the decision at match time)
    paths = outcome_paths(c, [m["mint"] for m in matches])
    for m in matches:
        pth = paths.get(m["mint"], [])
        m["path"] = pth
        m["net"] = trade_return(pth)
        m["max_1h"] = (max((p["price"] for p in pth if p["h"] <= 1), default=0) / pth[0]["price"] - 1) if pth else None
    # hit rates for the *next* period use the earlier outcomes (strictly causal in the live loop)
    for m in matches:
        if m["max_1h"] is not None:
            meta_hits[m["meta"]].append(1 if m["max_1h"] >= 0.5 else 0)
    return matches, metas_by_step


# ------------------------------------------------------------------ walk-forward evaluation
RULES: dict[str, list[dict[str, Any]]] = {
    "all matched": [],
    "shield pass": [{"f": "shield_ok", "op": ">=", "v": 1}],
    "shield pass & first": [{"f": "shield_ok", "op": ">=", "v": 1}, {"f": "first", "op": ">=", "v": 1}],
    "shield pass & early stage": [{"f": "shield_ok", "op": ">=", "v": 1}, {"f": "stage_early", "op": ">=", "v": 1}],
    "shield pass & X+launch cross": [{"f": "shield_ok", "op": ">=", "v": 1}, {"f": "cross", "op": ">=", "v": 1}],
    "shield pass & CA on X": [{"f": "shield_ok", "op": ">=", "v": 1}, {"f": "x_ca", "op": ">=", "v": 1}],
    "shield pass & liq>=5k": [{"f": "shield_ok", "op": ">=", "v": 1}, {"f": "liquidity_usd", "op": ">=", "v": 5000}],
    "shield pass & meta score>=0.6": [{"f": "shield_ok", "op": ">=", "v": 1}, {"f": "meta_score", "op": ">=", "v": 0.6}],
    "copycats (control)": [{"f": "first", "op": "<=", "v": 0}],
    "shield FAIL (control)": [{"f": "shield_ok", "op": "<=", "v": 0}],
}


def samples(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in matches:
        if m.get("net") is None:
            continue
        x = dict(m["features"], shield_ok=1.0 if m["shield_ok"] else 0.0)
        out.append({"id": m["mint"], "at": m["launched_at"].isoformat(), "x": x, "liq": x["liquidity_usd"],
                    "disposition": "meta", "net": m["net"], "gross": m["net"]})
    return sorted(out, key=lambda s: s["at"])


def walk_forward(smp: list[dict[str, Any]], min_n: int = 30) -> dict[str, Any]:
    if len(smp) < 2 * min_n:
        return {"status": "insufficient_data", "samples": len(smp), "rules": []}
    cut = int(len(smp) * 0.6)
    train, test = smp[:cut], smp[cut:]
    rows = []
    for i, (name, f) in enumerate(RULES.items()):
        tr = [s["net"] for s in train if em.rule_match(s["x"], f)]
        te = [s["net"] for s in test if em.rule_match(s["x"], f)]
        lo, hi, p = em.bootstrap(te, seed=i) if len(te) >= 2 else (float("nan"), float("nan"), 1.0)
        rows.append({"rule": name, "train_n": len(tr), "train_mean": round(em.mean(tr), 4) if tr else None,
                     "test_n": len(te), "test_mean": round(em.mean(te), 4) if te else None,
                     "ci95": [round(lo, 4), round(hi, 4)] if len(te) >= 2 else None, "p": p,
                     "hit_50pct": round(sum(v > 0.5 for v in te) / len(te), 3) if te else None,
                     "selected_on_train": bool(len(tr) >= min_n and em.mean(tr) > 0)})
    for r, a in zip(rows, em.bh_adjust([r["p"] for r in rows])):
        r["p_bh"] = round(a, 4)
        r["p"] = round(r["p"], 4)
        r["survives"] = bool(r["selected_on_train"] and r["test_n"] >= min_n and r["ci95"] and r["ci95"][0] > 0
                             and a < 0.05)
    return {"status": "ok", "samples": len(smp), "train_window": [train[0]["at"], train[-1]["at"]],
            "test_window": [test[0]["at"], test[-1]["at"]], "rules": rows,
            "costs": "2% fees + constant-product slippage on $25, upside capped +300%, adaptive meta exits",
            "survivors": [r["rule"] for r in rows if r["survives"]]}


# ------------------------------------------------------------------ live cycle (paper)
def _proven(life_db: str | None) -> bool:
    if not life_db or not os.path.isfile(life_db):
        return False
    c = sqlite3.connect(f"file:{life_db}?mode=ro", uri=True)
    try:
        return bool(c.execute("SELECT 1 FROM agents WHERE kind='meta_launch' AND status='earning'").fetchone())
    except sqlite3.Error:
        return False
    finally:
        c.close()


def latest_marks(c: sqlite3.Connection, mints: list[str]) -> dict[str, dict[str, float]]:
    """Last observation tick per mint, NO age check -> research/backtest only. Never mark live positions with it:
    the newest tick can be hours old, and a frozen price walks every position to its time stop at exactly -FEE."""
    paths = outcome_paths(c, mints)
    return {m: p[-1] for m, p in paths.items() if p}


MARK_MAX_AGE_S = float(os.getenv("NOEMA_META_MARK_MAX_AGE_S", "600"))
PUMP_SUPPLY_TOKENS = 1e9  # pump.fun fixed supply: price_sol = market_cap_sol / 1e9


def _age_s(now: datetime, at: str | None) -> float | None:
    t = _ts(at)
    return None if t is None else (now - t).total_seconds()


def obs_marks(c: sqlite3.Connection, mints: list[str], now: datetime) -> dict[str, dict[str, Any]]:
    """Newest observation tick per mint WITH its age (observed_at)."""
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(mints), 500):
        chunk = mints[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in c.execute(f"SELECT mint, observed_at, tick_json FROM trench_observations WHERE mint IN ({q})", chunk):
            try:
                t = json.loads(r["tick_json"])
                p = float(t.get("price_usd") or 0)
            except (TypeError, ValueError):
                continue
            age = _age_s(now, r["observed_at"])
            if p <= 0 or age is None or age < -60:
                continue
            if r["mint"] not in out or age < out[r["mint"]]["age_s"]:
                out[r["mint"]] = {"price": p, "liq": t.get("liquidity_usd") or 0,
                                  "vol": (t.get("buy_volume_usd") or 0) + (t.get("sell_volume_usd") or 0),
                                  "age_s": max(age, 0.0), "src": "obs"}
    return out


def trade_mcap(c: sqlite3.Connection, mint: str, *, before: str | None = None, after: str | None = None
               ) -> tuple[float, str] | None:
    """A live pump.fun bonding-curve trade's market_cap_sol (launch_trades, Helius/PumpPortal) and its time."""
    try:
        if before is not None:
            r = c.execute("SELECT market_cap_sol, received_at FROM launch_trades WHERE mint=? AND received_at<=? "
                          "AND market_cap_sol>0 ORDER BY received_at DESC LIMIT 1", (mint, before)).fetchone()
        else:
            r = c.execute("SELECT market_cap_sol, received_at FROM launch_trades WHERE mint=? AND received_at>=? "
                          "AND market_cap_sol>0 ORDER BY received_at ASC LIMIT 1", (mint, after or "")).fetchone()
    except sqlite3.Error:  # no launch_trades table (tests / research copies)
        return None
    return (float(r[0]), r[1]) if r else None


def fresh_mark(c: sqlite3.Connection, mint: str, entry: float | None, anchor: dict[str, Any] | None,
               obs: dict[str, Any] | None, now: datetime, max_age_s: float = MARK_MAX_AGE_S) -> dict[str, Any] | None:
    """Freshest of (observation tick, live trade) no older than max_age_s, else None (refuse to mark).
    Trade marks are relative to an anchor trade mapped onto the USD entry, so SOL/USD never enters the ratio."""
    best = obs if obs and obs["age_s"] <= max_age_s else None
    if entry and anchor and anchor.get("mcap"):
        tm = trade_mcap(c, mint, before=now.isoformat())
        if tm:
            age = _age_s(now, tm[1])
            if age is not None and age <= max_age_s and (best is None or age < best["age_s"]):
                best = {"price": entry * tm[0] / anchor["mcap"], "liq": None, "vol": None,
                        "age_s": max(age, 0.0), "src": "launch_trades"}
    return best


def trade_anchor(c: sqlite3.Connection, mint: str, opened_at: str, max_gap_s: float = MARK_MAX_AGE_S
                 ) -> dict[str, Any] | None:
    """The live trade closest to the entry (at/before, else first after) within max_gap_s."""
    opened = _ts(opened_at)
    if opened is None:
        return None
    best = None
    for tm in (trade_mcap(c, mint, before=opened_at), trade_mcap(c, mint, after=opened_at)):
        if tm:
            t = _ts(tm[1])
            gap = abs((t - opened).total_seconds()) if t else 1e18
            if gap <= max_gap_s and (best is None or gap < best[1]):
                best = (tm[0], gap)
    return {"mcap": best[0]} if best else None


def cycle(db: str, meta_db: str, life_db: str | None = None, now: datetime | None = None,
          backtest_hours: float = 12) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    src = _ro(db)
    out = connect(meta_db)
    try:
        matches, steps = replay(src, now - timedelta(hours=backtest_hours), now)
        cur = steps[max(steps)] if steps else []
        iso = now.isoformat()
        for m in cur:
            out.execute("INSERT OR REPLACE INTO metas VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (m["meta"], iso, json.dumps(m["terms"]), m["stage"], m["x_now"], m["launches_now"],
                         m["culture_now"], m["velocity"], m["accel"], m["hit_rate"], m["score"]))
        out.execute("DELETE FROM metas WHERE at < ?", ((now - timedelta(days=7)).isoformat(),))
        for m in matches:
            outcome = {"net": m["net"], "max_1h": m["max_1h"],
                       "path": [(round(p["h"], 3), p["price"]) for p in m["path"]]}
            out.execute("INSERT OR REPLACE INTO meta_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (m["mint"], m["meta"], m["matched_at"].isoformat(), m["launched_at"].isoformat(), m["name"],
                         m["symbol"], m["rank"], m["sim"], m["x_ca"], int(m["shield_ok"]), json.dumps(m["shield"]),
                         json.dumps(m["features"]), json.dumps(outcome)))
        # paper entries: only fresh (matched in the last step), shield-pass, first-per-meta, meta not fading
        size = round(0.03 * BANKROLL, 2) if _proven(life_db) else PROBE_USD
        tier = "proven" if size > PROBE_USD else "probe"
        for m in ([] if retired() else matches):  # retired: no new entries, open ones are still managed below
            if (m["shield_ok"] and m["rank"] == "first" and m["stage"] != "fading"
                    and m["matched_at"] >= now - timedelta(minutes=10)):
                mk = obs_marks(src, [m["mint"]], now).get(m["mint"])
                if mk and mk["age_s"] <= MARK_MAX_AGE_S:  # never open on a stale price
                    out.execute("INSERT OR IGNORE INTO meta_positions (mint, meta, opened_at, entry, size_usd, tier,"
                                " state_json, status) VALUES (?,?,?,?,?,?,?, 'open')",
                                (m["mint"], m["meta"], iso, mk["price"], size, tier, "{}"))
        # manage open paper positions with adaptive exits; time stop extends only while the meta isn't fading
        live_stage = {m["meta"]: m["stage"] for m in cur}
        opens = out.execute("SELECT * FROM meta_positions WHERE status='open'").fetchall()
        obs = obs_marks(src, [p["mint"] for p in opens], now)
        for p in opens:
            raw = json.loads(p["state_json"] or "{}")
            age_h = (now - datetime.fromisoformat(p["opened_at"])).total_seconds() / 3600
            if not raw.get("anchor"):
                anchor = trade_anchor(src, p["mint"], p["opened_at"])
                if anchor:
                    raw["anchor"] = anchor
            mk = fresh_mark(src, p["mint"], p["entry"], raw.get("anchor"), obs.get(p["mint"]), now)
            if not mk:
                # refuse to mark on a stale price; if nothing fresh arrives by max hold, close UNPRICED
                raw["stale_cycles"] = raw.get("stale_cycles", 0) + 1
                if age_h >= META_EXIT["max_hold_h"]:
                    out.execute("UPDATE meta_positions SET status='closed', closed_at=?, reason=?, realized_ret=NULL,"
                                " state_json=? WHERE id=?", (iso, "no fresh price (unpriced, excluded from P&L)",
                                                             json.dumps(raw), p["id"]))
                else:
                    out.execute("UPDATE meta_positions SET state_json=? WHERE id=?", (json.dumps(raw), p["id"]))
                continue
            st = ax.PosState(entry=p["entry"], **{k: v for k, v in raw.items() if k in
                                                  ("remaining", "tp_hit", "peak", "liq_high", "vol_high", "rets",
                                                   "last_price", "extended_h")})
            st.last_price = st.last_price or p["entry"]
            params = ax.AdaptiveExit.from_genome(META_EXIT)
            if live_stage.get(p["meta"], "fading") == "fading":
                params.extend_h = 0.0
            realized = raw.get("realized", 0.0)
            frac, why = ax.decide(st, params, price=mk["price"], age_h=age_h, liquidity=mk.get("liq"),
                                  volume=mk.get("vol"))
            realized += frac * mk["price"] / p["entry"]
            state = {"remaining": st.remaining, "tp_hit": st.tp_hit, "peak": st.peak, "liq_high": st.liq_high,
                     "vol_high": st.vol_high, "rets": st.rets[-20:], "last_price": st.last_price,
                     "extended_h": st.extended_h, "realized": realized, "anchor": raw.get("anchor"),
                     "stale_cycles": raw.get("stale_cycles", 0), "mark_src": mk["src"],
                     "mark_age_s": round(mk["age_s"], 1)}
            if st.remaining <= 1e-9:
                out.execute("UPDATE meta_positions SET status='closed', closed_at=?, reason=?, realized_ret=?, "
                            "state_json=? WHERE id=?", (iso, why, round(realized - 1 - FEE, 4), json.dumps(state), p["id"]))
            else:
                out.execute("UPDATE meta_positions SET state_json=? WHERE id=?", (json.dumps(state), p["id"]))
        wf = walk_forward(samples(matches))
        out.execute("INSERT INTO meta_runs VALUES (?,?)", (iso, json.dumps({"matches": len(matches), "wf": wf})))
        out.execute("DELETE FROM meta_runs WHERE at < ?", ((now - timedelta(days=2)).isoformat(),))
        out.commit()
        return {"metas": len(cur), "matches": len(matches), "walk_forward": wf.get("status"),
                "survivors": wf.get("survivors", [])}
    finally:
        src.close()
        out.close()


SHIELD_GAPS = ["wallet-level bundle/sniper detection (we only see holder counts, not first-block buyer wallets)",
               "clustered funding sources (no funding-graph data)", "dev wallet rug history beyond the launchpad's devMints count"]


def board(meta_db: str) -> dict[str, Any]:
    out: dict[str, Any] = {"schema": "tev.metas.v1", "mode": "paper only", "retired": retired(), "metas": [], "matches": [],
                           "rejections": [], "positions": [], "walk_forward": None, "shield_gaps": SHIELD_GAPS}
    if not os.path.isfile(meta_db):
        return out
    c = _ro(meta_db)
    try:
        last = c.execute("SELECT MAX(at) FROM metas").fetchone()[0]
        out["as_of"] = last
        out["metas"] = [dict(r, terms=json.loads(r["terms"])) for r in
                        c.execute("SELECT * FROM metas WHERE at=? ORDER BY score DESC", (last,))]
        rows = [dict(r) for r in c.execute("SELECT * FROM meta_matches ORDER BY matched_at DESC LIMIT 60")]
        for r in rows:
            r["shield"] = json.loads(r.pop("shield_json") or "[]")
            r["features"] = json.loads(r.pop("features_json") or "{}")
            oc = json.loads(r.pop("outcome_json") or "{}")
            r["net"], r["max_1h"] = oc.get("net"), oc.get("max_1h")
        out["matches"] = [r for r in rows if r["shield_ok"]][:25]
        out["rejections"] = [r for r in rows if not r["shield_ok"]][:25]
        cnt = Counter()
        for (sj,) in c.execute("SELECT shield_json FROM meta_matches WHERE shield_ok=0"):
            for w in json.loads(sj or "[]"):
                cnt[re.sub(r"[\d.]+", "#", w)[:40]] += 1
        out["rejection_reasons"] = cnt.most_common(8)
        out["counts"] = dict(c.execute("SELECT 'matched', COUNT(*) FROM meta_matches UNION ALL "
                                       "SELECT 'shield_pass', COUNT(*) FROM meta_matches WHERE shield_ok=1").fetchall())
        out["positions"] = [dict(r) for r in c.execute("SELECT id, mint, meta, opened_at, entry, size_usd, tier, status,"
                                                       " closed_at, reason, realized_ret FROM meta_positions ORDER BY id DESC LIMIT 20")]
        run = c.execute("SELECT note FROM meta_runs ORDER BY at DESC LIMIT 1").fetchone()
        out["walk_forward"] = json.loads(run[0])["wf"] if run else None
    finally:
        c.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--meta-db", default=os.getenv("NOEMA_META_DB", "data/meta_launch.db"))
    ap.add_argument("--life-db", default=os.getenv("NOEMA_LIFECYCLE_DB", "data/agent_lifecycle.db"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=int(os.getenv("NOEMA_META_INTERVAL_SECONDS", "300")))
    a = ap.parse_args()
    while True:
        try:
            print(datetime.now(UTC).isoformat(), json.dumps(cycle(a.db, a.meta_db, a.life_db)), flush=True)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            print("meta cycle error:", repr(e), flush=True)
        if not a.loop:
            break
        from .agent_lifecycle import throttle_factor
        time.sleep(a.interval * throttle_factor(a.life_db, "meta_launch"))


if __name__ == "__main__":
    main()
