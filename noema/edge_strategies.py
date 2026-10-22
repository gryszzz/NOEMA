"""Structural-edge strategy book. PAPER ONLY.

Six strategies, ordered by expected edge. Every signal is paper-traded, recorded in
the accuracy ledger (agent ``strategy:<name>``) and settled from official outcomes or
the latest venue quote. Nothing here can place a live order (``LIVE_SUPPORTED``).

1. ``pm_arb``            Cross-venue (Kalshi vs Polymarket US) gaps and internal ladder /
                          bracket inconsistencies whose locked payoff beats fees + slippage.
2. ``news_latency``      Primary-source / X "not priced in" alerts with a direction on a
                          market that has not repriced yet. Tight time stop.
3. ``fav_longshot``      Price-bucket bias measured walk-forward on resolved Kalshi history;
                          traded only in buckets whose out-of-sample EV after fees is > 0.
4. ``smart_money``       Wallets scored by realized PnL from public GeckoTerminal pool trades
                          on trending tokens; followed only with a long, significant record.
5. ``meme_catalyst``     Memecoins need a real catalyst (see :func:`catalyst_screen`), wash
                          and bundle checks; second-leg breakouts on established tokens.
   ``majors_momentum``   BTC/ETH/SOL trend sleeve with a regime filter.
6. Allocator             Paper capital by shrunk EV/Sharpe from settled signals; losers benched.

Quotes are top-of-book only (no depth in the snapshot), so arb sizes are capped small and
any gap is reported as a *candidate*; cross-venue matches carry a resolution-review flag.
Missing data -> no signal (fail closed). No lookahead: backtests only use quotes stamped
before settlement and rules fitted on earlier data than they are tested on.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

LIVE_SUPPORTED = False
STRATEGIES = ("pm_arb", "news_latency", "fav_longshot", "smart_money", "meme_catalyst", "majors_momentum")
LABELS = {"pm_arb": "Prediction arb / relative value", "news_latency": "News latency",
          "fav_longshot": "Favourite-longshot bias", "smart_money": "Smart-money copy",
          "meme_catalyst": "Memecoin catalyst", "majors_momentum": "Majors momentum"}
GT = "https://api.geckoterminal.com/api/v2"
CB = "https://api.exchange.coinbase.com"
KALSHI = "kalshi:production"
PMUS = "polymarket-us"
SHRINK_K = 30.0
MIN_BENCH_N = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
    strategy TEXT NOT NULL, key TEXT NOT NULL, title TEXT, legs_json TEXT NOT NULL, cost REAL NOT NULL,
    locked_payoff REAL, edge REAL, size_usd REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    reason TEXT, meta_json TEXT NOT NULL DEFAULT '{}', forecast_id INTEGER, expires_at TEXT,
    closed_at TEXT, exit_value REAL, pnl_usd REAL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_open_key ON strategy_signals(strategy, key) WHERE status='open';
CREATE TABLE IF NOT EXISTS strategy_backtests (strategy TEXT PRIMARY KEY, run_at TEXT, n INTEGER, ev REAL,
    hit_rate REAL, sharpe REAL, verdict TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS strategy_alloc (strategy TEXT PRIMARY KEY, at TEXT, weight REAL, status TEXT,
    reason TEXT, n INTEGER, ev REAL, sharpe REAL, pnl_usd REAL);
CREATE TABLE IF NOT EXISTS wallet_trades (chain TEXT, wallet TEXT, token TEXT, tx TEXT, at TEXT, side TEXT,
    usd REAL, price REAL, PRIMARY KEY (chain, tx, wallet, token, side));
CREATE TABLE IF NOT EXISTS wallet_scores (chain TEXT, wallet TEXT, at TEXT, tokens INTEGER, closed INTEGER,
    pnl_usd REAL, mean_ret REAL, t_stat REAL, win_rate REAL, followed INTEGER, PRIMARY KEY (chain, wallet));
CREATE TABLE IF NOT EXISTS majors_state (symbol TEXT PRIMARY KEY, at TEXT, close REAL, sma20 REAL,
    mom7 REAL, regime TEXT);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _f(v: Any) -> float | None:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _env(name: str, default: float) -> float:
    return _f(os.getenv(name)) if _f(os.getenv(name)) is not None else default


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ------------------------------------------------------------------------------- costs

def kalshi_fee(p: float) -> float:
    """Kalshi taker fee per contract (0.07 * P * (1-P)); rounding up per order is ignored for
    100-lot orders, which is why a slippage allowance is charged on top."""
    return 0.07 * p * (1 - p)


PMUS_TAKER_THETA = 0.0695  # https://docs.polymarket.us/fees (standard taker, effective 2026-09-17 per CFTC filing)


def pmus_fee(p: float) -> float:
    """Polymarket US standard taker fee per contract: theta x p x (1-p), theta 0.0695
    (0.10 on table tennis, ignored here). Source: https://docs.polymarket.us/fees and the
    2026-09-21 CFTC weekly notification (rules09212628660.pdf). NOEMA_PMUS_FEE_PER_CONTRACT,
    if set, overrides with a flat per-contract fee for what-if checks."""
    flat = os.getenv("NOEMA_PMUS_FEE_PER_CONTRACT")
    if flat:
        try:
            return float(flat)
        except ValueError:
            pass
    return _env("NOEMA_PMUS_TAKER_THETA", PMUS_TAKER_THETA) * p * (1 - p)


def entry_buffer() -> float:
    """Minimum expected gain per $1 contract AFTER fees (and slippage) before any paper agent enters.
    NOEMA_ENTRY_BUFFER, default 0.01 (1 cent). Signals below it are recorded as shadow only."""
    return max(0.0, _env("NOEMA_ENTRY_BUFFER", 0.01))


def clears_fee_buffer(edge_after_fees: float | None) -> bool:
    """Fee-plus-buffer gate: True only when the post-fee expected gain is known and >= the buffer."""
    return edge_after_fees is not None and edge_after_fees >= entry_buffer()


def leg_cost(venue: str, price: float) -> float:
    slip = _env("NOEMA_STRATEGY_SLIPPAGE", 0.005)
    fee = kalshi_fee(price) if venue.startswith("kalshi") else pmus_fee(price)
    return price + fee + slip


# ------------------------------------------------------------------------- 1. arbitrage

_STRIKE = re.compile(r"^(?P<kind>[TB])(?P<v>-?\d+(?:\.\d+)?)$")


def _quotes(conn: sqlite3.Connection, now: datetime, max_age_min: float = 60) -> list[dict[str, Any]]:
    cutoff = (now - timedelta(minutes=max_age_min)).isoformat()
    out = []
    for venue, mid, captured, snap in conn.execute(
            "SELECT venue, market_id, captured_at, snapshot_json FROM market_snapshots WHERE valid=1 AND captured_at>=?",
            (cutoff,)):
        try:
            s = json.loads(snap)
        except ValueError:
            continue
        q = {k: _f(s.get(k)) for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")}
        if any(v is None or not 0 < v < 1 for v in q.values()):
            continue  # one-sided or empty book -> unpriceable
        close = _ts(s.get("closes_at"))
        if close is not None and close <= now:
            continue  # closed market: quote is not tradeable
        at = _ts(captured)
        out.append({"venue": venue, "market_id": mid, "title": str(s.get("title") or ""),
                    "at": at.timestamp() if at else None,
                    "rules": str(s.get("resolution_rules") or ""), "closes_at": s.get("closes_at"), **q})
    return out


MAX_LEG_SKEW_S = 90.0


def synchronous(*qs: dict[str, Any]) -> bool:
    """Arb legs must be quoted at (nearly) the same time; stale-vs-fresh pairs fake an arb."""
    ts = [q.get("at") for q in qs]
    if any(t is None for t in ts):
        return "at" not in qs[0]  # unit-test quotes without timestamps
    return max(ts) - min(ts) <= MAX_LEG_SKEW_S


def strike_direction(title: str) -> str | None:
    """Direction from the market's own title only; anything ambiguous is skipped (fail closed)."""
    t = title.lower()
    up = bool(re.search(r">|\babove\b|greater than|or (more|higher|above)", t))
    down = bool(re.search(r"<|\bbelow\b|less than|or (less|lower|below)", t))
    return "above" if up and not down else "below" if down and not up else None


def ladder_arbs(quotes: list[dict[str, Any]], *, min_edge: float = 0.01) -> list[dict[str, Any]]:
    """Kalshi strike ladders inside one event.

    * ``T`` (above-K) ladder: YES(K_lo) >= YES(K_hi) must hold. If YES_ask(K_lo) + NO_ask(K_hi)
      plus costs < 1, holding both pays >= 1 in every state -> locked edge.
    * ``B`` brackets are mutually exclusive: at most one resolves YES, so buying NO on all n
      pays >= n-1. Locked if sum(NO cost) < n-1.
    """
    events: dict[str, list[tuple[str, float, dict[str, Any]]]] = defaultdict(list)
    for q in quotes:
        if not q["venue"].startswith("kalshi"):
            continue
        event, _, suffix = q["market_id"].rpartition("-")
        m = _STRIKE.match(suffix)
        if event and m:
            events[event].append((m["kind"], float(m["v"]), q))
    out = []
    for event, rows in events.items():
        ladders: dict[str, list[tuple[str, float, dict[str, Any]]]] = {"above": [], "below": []}
        for r in rows:
            if r[0] == "T" and (d := strike_direction(r[2]["title"])):
                ladders[d].append(r)
        for direction, ladder in ladders.items():
            ladder.sort(key=lambda r: r[1])
            for i, (_, k_lo, lo) in enumerate(ladder):
                for _, k_hi, hi in ladder[i + 1:]:
                    # above: YES(lo) covers YES(hi). below: YES(hi) covers YES(lo).
                    wide, narrow = (lo, hi) if direction == "above" else (hi, lo)
                    if not synchronous(wide, narrow):
                        continue
                    cost = leg_cost(KALSHI, wide["yes_ask"]) + leg_cost(KALSHI, narrow["no_ask"])
                    if 1 - cost >= min_edge:
                        sym = ">" if direction == "above" else "<"
                        out.append({"strategy": "pm_arb", "key": f"ladder:{wide['market_id']}|{narrow['market_id']}",
                                    "title": f"{event}: YES {sym}{wide['market_id'].rsplit('-', 1)[1][1:]} "
                                             f"@{wide['yes_ask']:.2f} + NO {sym}{narrow['market_id'].rsplit('-', 1)[1][1:]}"
                                             f" @{narrow['no_ask']:.2f}",
                                    "kind": "ladder_monotonicity", "cost": round(cost, 4), "locked_payoff": 1.0,
                                    "edge": round(1 - cost, 4),
                                    "legs": [{"venue": KALSHI, "market_id": wide["market_id"], "side": "yes",
                                              "price": wide["yes_ask"]},
                                             {"venue": KALSHI, "market_id": narrow["market_id"], "side": "no",
                                              "price": narrow["no_ask"]}]})
        brackets = [r for r in rows if r[0] == "B"]
        if len(brackets) >= 3 and synchronous(*[r[2] for r in brackets]):
            cost = sum(leg_cost(KALSHI, r[2]["no_ask"]) for r in brackets)
            payoff = len(brackets) - 1
            if payoff - cost >= min_edge:
                out.append({"strategy": "pm_arb", "key": f"bracket:{event}", "kind": "bracket_sum",
                            "title": f"{event}: NO on all {len(brackets)} brackets", "cost": round(cost, 4),
                            "locked_payoff": float(payoff), "edge": round(payoff - cost, 4),
                            "legs": [{"venue": KALSHI, "market_id": r[2]["market_id"], "side": "no",
                                      "price": r[2]["no_ask"]} for r in brackets]})
    return out


_STOP = {"the", "a", "an", "of", "to", "in", "on", "at", "for", "by", "will", "be", "is", "if", "this", "market", "settle", "yes", "no", "wins", "win", "than", "or", "and", "with", "from", "as", "who", "what", "which", "party", "nominee", "election", "midterm", "us", "u.s.", "2026", "2027", "resolve", "resolves"}


def _norm_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+(?:\.\d+)?", text.lower()) if w not in _STOP and len(w) > 1}


def _dates(text: str) -> set[str]:
    return set(re.findall(r"20\d\d-\d\d-\d\d", text)) | set(re.findall(
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}", text.lower()))


def cross_venue_gaps(quotes: list[dict[str, Any]], *, min_sim: float = 0.6,
                     min_edge: float = 0.02) -> list[dict[str, Any]]:
    """Strict title match Kalshi <-> Polymarket US; gap must clear both venues' costs.
    Matches are candidates: different resolution wording can make them non-equivalent."""
    k = [q for q in quotes if q["venue"].startswith("kalshi")]
    p = [q for q in quotes if q["venue"] == PMUS]
    index: dict[str, list[int]] = defaultdict(list)
    ktok = []
    for i, q in enumerate(k):
        t = _norm_tokens(q["title"])
        ktok.append(t)
        for w in t:
            index[w].append(i)
    out = []
    for pq in p:
        pt = _norm_tokens(pq["title"])
        if len(pt) < 3:
            continue
        hits: dict[int, int] = defaultdict(int)
        for w in pt:
            for i in index.get(w, ()):
                hits[i] += 1
        for i, inter in hits.items():
            sim = inter / len(pt | ktok[i])
            if sim < min_sim:
                continue
            kq = k[i]
            if not synchronous(pq, kq):
                continue
            pd, kd = _dates(pq["market_id"] + " " + pq["title"]), _dates(kq["title"] + " " + kq["rules"])
            if pd and kd and not pd & kd:
                continue
            for a, b in ((pq, kq), (kq, pq)):
                cost = leg_cost(a["venue"], a["yes_ask"]) + leg_cost(b["venue"], b["no_ask"])
                if 1 - cost >= min_edge:
                    out.append({"strategy": "pm_arb", "kind": "cross_venue", "similarity": round(sim, 3),
                                "key": f"xv:{a['venue']}:{a['market_id']}|{b['market_id']}",
                                "title": f"YES {a['title'][:60]} ({a['venue']}) + NO on {b['venue']}",
                                "cost": round(cost, 4), "locked_payoff": 1.0, "edge": round(1 - cost, 4),
                                "needs_resolution_review": True,
                                "legs": [{"venue": a["venue"], "market_id": a["market_id"], "side": "yes",
                                          "price": a["yes_ask"]},
                                         {"venue": b["venue"], "market_id": b["market_id"], "side": "no",
                                          "price": b["no_ask"]}]})
    return out


# ---------------------------------------------------------------------- 2. news latency

PRIMARY_DOMAINS = ("federalreserve.gov", "bls.gov", "whitehouse.gov", "federalregister.gov", "courtlistener.com",
                   "supremecourt.gov", "justice.gov", "congress.gov", "sec.gov", "treasury.gov", "nhc.noaa.gov")


def news_latency_signals(conn: sqlite3.Connection, quotes: list[dict[str, Any]], now: datetime, *,
                         window_min: float = 60, max_move: float = 0.03, hold_hours: float = 6) -> list[dict[str, Any]]:
    """Directional alerts only: an X burst flagged ``not_priced_in`` with a direction, or a
    primary-source article with a confident model stance. Entered only if the quote has not
    already moved ``max_move`` toward the news."""
    by = {(q["venue"], q["market_id"]): q for q in quotes}
    since = (now - timedelta(minutes=window_min)).isoformat()
    cands: list[tuple[str, str, float, str, float | None, str]] = []
    try:
        for venue, mid, direction, title, then, src in conn.execute(
                "SELECT venue, market_id, direction, title, price_then, source FROM x_bursts WHERE at>=? "
                "AND status='not_priced_in' AND direction IS NOT NULL AND direction<>0 AND market_id IS NOT NULL",
                (since,)):
            cands.append((venue, mid, float(direction), title or "", _f(then), f"x_burst:{src}"))
    except sqlite3.Error:
        pass
    try:
        for venue, mid, stance, conf, title in conn.execute(
                "SELECT s.venue, s.market_id, s.stance, s.confidence, l.title FROM x_stance s "
                "JOIN news_market_links l ON l.market_id=s.market_id AND l.venue=s.venue "
                "JOIN news_evidence e ON e.evidence_id=l.evidence_id WHERE s.at>=? AND s.relevant=1 "
                "AND s.confidence>=0.7 AND ABS(s.stance)>=0.5 AND (" +
                " OR ".join("e.url LIKE ?" for _ in PRIMARY_DOMAINS) + ")",
                (since, *[f"%{d}%" for d in PRIMARY_DOMAINS])):
            cands.append((venue, mid, float(stance), title or "", None, "primary_source"))
    except sqlite3.Error:
        pass  # schema differs -> that source simply contributes nothing
    out = []
    for venue, mid, direction, title, then, src in cands:
        q = by.get((venue, mid))
        if not q:
            continue
        side = "yes" if direction > 0 else "no"
        price = q["yes_ask"] if side == "yes" else q["no_ask"]
        if then is not None:
            moved = (q["yes_bid"] + q["yes_ask"]) / 2 - then
            if moved * (1 if side == "yes" else -1) >= max_move:
                continue  # already repriced
        cost = leg_cost(venue, price)
        out.append({"strategy": "news_latency", "key": f"news:{venue}:{mid}:{side}", "title": title[:120],
                    "kind": src, "cost": round(cost, 4), "locked_payoff": None, "edge": None,
                    "expires_at": (now + timedelta(hours=hold_hours)).isoformat(),
                    "legs": [{"venue": venue, "market_id": mid, "side": side, "price": price}],
                    "fair": min(0.99, max(0.01, (price + 0.05 * abs(direction))))})
    return out


# ------------------------------------------------------------------ 3. favourite-longshot

BUCKETS = (0.0, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 1.0)


def _bucket(p: float) -> int:
    for i in range(len(BUCKETS) - 1):
        if BUCKETS[i] <= p < BUCKETS[i + 1]:
            return i
    return len(BUCKETS) - 2


def market_family(ticker: str) -> str:
    s = ticker.upper()
    if re.match(r"KX(NHL|NBA|NFL|MLB|ITF|ATP|WTA|J\d?LEAGUE|EPL|UCL|ESOCCER|LIGA|MLS|NCAA|UFC|PGA|WNBA|F1|SERIEA)", s):
        return "sports"
    if re.match(r"KX(BTC|ETH|SOL|XRP|DOGE|SHIBA)", s):
        return "crypto"
    if re.match(r"KX(AAAGAS|GAS)", s):
        return "gas"
    if re.match(r"KX(INX|NASDAQ|SPX|DOW|USDJPY|EURUSD|WTI|GOLD)", s):
        return "index_fx"
    if re.match(r"KX(CPI|FED|GDP|PAYROLL|JOBLESS|UNRATE|PCE)", s):
        return "macro"
    return "other"


def history_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Resolved Kalshi markets with the prior-day book (previous_yes_bid/ask) Kalshi stores on the
    market. Only quotes with a 1-10c spread on both sides are used; the quote predates settlement."""
    out = []
    for mid, y, raw in conn.execute("SELECT market_id, outcome_yes, raw_json FROM outcomes WHERE venue=?", (KALSHI,)):
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        b, a = _f(r.get("previous_yes_bid_dollars")), _f(r.get("previous_yes_ask_dollars"))
        close = _ts(r.get("close_time"))
        if not b or not a or not 0 < b < a < 1 or a - b > 0.10 or close is None:
            continue
        out.append({"market_id": mid, "family": market_family(mid), "event": str(r.get("event_ticker") or mid),
                    "bid": b, "ask": a, "mid": (a + b) / 2, "y": int(y), "close": close})
    out.sort(key=lambda r: r["close"])
    return out


def _side_pnl(row: dict[str, Any], side: str) -> float:
    if side == "yes":
        return row["y"] - leg_cost(KALSHI, row["ask"])
    return (1 - row["y"]) - leg_cost(KALSHI, 1 - row["bid"])


def fit_bias_rules(train: list[dict[str, Any]], *, min_n: int = 40, margin: float = 0.01) -> dict[tuple, str]:
    """(family, bucket) -> side, kept only if in-sample mean PnL per contract after costs > margin."""
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in train:
        groups[(r["family"], _bucket(r["mid"]))].append(r)
        groups[("*", _bucket(r["mid"]))].append(r)
    rules = {}
    for g, rows in groups.items():
        if len(rows) < min_n:
            continue
        for side in ("yes", "no"):
            if sum(_side_pnl(r, side) for r in rows) / len(rows) > margin:
                rules[g] = side
    return rules


def _apply_rule(rules: dict[tuple, str], family: str, p: float) -> tuple[str, tuple] | None:
    b = _bucket(p)
    for g in ((family, b), ("*", b)):
        if g in rules:
            return rules[g], g
    return None


def _summary(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    if not n:
        return {"n": 0, "ev": None, "hit_rate": None, "sharpe": None, "t_stat": None}
    mean = sum(pnls) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "ev": round(mean, 4), "hit_rate": round(sum(1 for x in pnls if x > 0) / n, 3),
            "sharpe": round(mean / sd, 3) if sd else None, "t_stat": round(mean / sd * math.sqrt(n), 2) if sd else None}


def backtest_fav_longshot(rows: list[dict[str, Any]], *, train_frac: float = 0.6) -> dict[str, Any]:
    """Walk-forward: rules from the first ``train_frac`` (by close time), scored on the rest.
    One trade per event (first market in it) to limit correlated outcomes."""
    if len(rows) < 100:
        return {"verdict": "insufficient_history", "n_rows": len(rows), **_summary([])}
    cut = int(len(rows) * train_frac)
    rules = fit_bias_rules(rows[:cut])
    pnls, by_rule, seen = [], defaultdict(list), set()
    for r in rows[cut:]:
        hit = _apply_rule(rules, r["family"], r["mid"])
        if not hit or r["event"] in seen:
            continue
        seen.add(r["event"])
        x = _side_pnl(r, hit[0])
        pnls.append(x)
        by_rule[f"{hit[1][0]}|{BUCKETS[hit[1][1]]:.2f}-{BUCKETS[hit[1][1] + 1]:.2f}|{hit[0]}"].append(x)
    calib = defaultdict(lambda: [0, 0.0, 0])
    for r in rows:
        c = calib[_bucket(r["mid"])]
        c[0] += 1
        c[1] += r["mid"]
        c[2] += r["y"]
    s = _summary(pnls)
    positive_rules = {k: _summary(v) for k, v in by_rule.items()}
    live_rules = [k for k, v in positive_rules.items() if v["n"] >= 20 and (v["ev"] or 0) > 0]
    verdict = ("positive_oos" if s["n"] >= 30 and (s["ev"] or 0) > 0 and (s["t_stat"] or 0) >= 2
               else "weak_positive_oos" if s["n"] and (s["ev"] or 0) > 0 else "no_edge_after_fees")
    return {"verdict": verdict, "n_rows": len(rows), "train": cut, "test": len(rows) - cut, **s,
            "rules_fitted": len(rules), "by_rule": positive_rules, "live_rules": live_rules,
            "calibration": [{"bucket": f"{BUCKETS[b]:.2f}-{BUCKETS[b + 1]:.2f}", "n": c[0],
                             "avg_price": round(c[1] / c[0], 3), "yes_rate": round(c[2] / c[0], 3)}
                            for b, c in sorted(calib.items())],
            "caveat": "prior-day Kalshi quote (previous_yes_bid/ask); top-of-book only; sports-heavy sample"}


def fav_longshot_signals(quotes: list[dict[str, Any]], bt: dict[str, Any], rows: list[dict[str, Any]],
                         now: datetime) -> list[dict[str, Any]]:
    """Trade a rule only if it was positive out of sample; rules refit on all history."""
    live = set(bt.get("live_rules") or [])
    if not live:
        return []
    rules = fit_bias_rules(rows)
    out = []
    for q in quotes:
        if not q["venue"].startswith("kalshi"):
            continue
        close = _ts(q.get("closes_at"))
        if close is None or not now < close <= now + timedelta(days=2):
            continue  # history is ~1 day before close; trade the same horizon
        mid = (q["yes_bid"] + q["yes_ask"]) / 2
        if q["yes_ask"] - q["yes_bid"] > 0.10:
            continue
        fam = market_family(q["market_id"])
        hit = _apply_rule(rules, fam, mid)
        if not hit:
            continue
        name = f"{hit[1][0]}|{BUCKETS[hit[1][1]]:.2f}-{BUCKETS[hit[1][1] + 1]:.2f}|{hit[0]}"
        if name not in live:
            continue
        ev = (bt["by_rule"].get(name) or {}).get("ev")
        price = q["yes_ask"] if hit[0] == "yes" else q["no_ask"]
        out.append({"strategy": "fav_longshot", "key": f"fl:{q['market_id']}", "title": q["title"][:120],
                    "kind": name, "cost": round(leg_cost(KALSHI, price), 4), "locked_payoff": None,
                    "edge": ev, "legs": [{"venue": KALSHI, "market_id": q["market_id"], "side": hit[0],
                                          "price": price}],
                    "fair": min(0.99, price + max(0.0, ev or 0))})
    out.sort(key=lambda s: -(s["edge"] or 0))
    return out[:10]


# ---------------------------------------------------------------------- 4. smart money

def ingest_trades(conn: sqlite3.Connection, chain: str, token: str, trades: list[dict[str, Any]]) -> int:
    n = 0
    for t in trades:
        a = t.get("attributes") if isinstance(t.get("attributes"), dict) else t
        who, kind, tx = a.get("tx_from_address"), a.get("kind"), a.get("tx_hash")
        usd = _f(a.get("volume_in_usd"))
        price = _f(a.get("price_to_in_usd") if kind == "buy" else a.get("price_from_in_usd"))
        if not who or kind not in ("buy", "sell") or not tx or not usd:
            continue
        n += conn.execute("INSERT OR IGNORE INTO wallet_trades VALUES (?,?,?,?,?,?,?,?)",
                          (chain, who, token, tx, a.get("block_timestamp"), kind, usd, price)).rowcount
    return n


def score_wallets(conn: sqlite3.Connection, now: datetime, *, min_closed: int = 15,
                  min_t: float = 2.0) -> dict[str, Any]:
    """Per wallet, per token: realized return = sold USD / bought USD - 1 on tokens where it both
    bought and sold (round trips seen in public trades). Followed only with >= ``min_closed``
    round trips, t-stat >= ``min_t`` and positive total PnL. Unrealized bags are not counted as wins."""
    flows: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for chain, wallet, token, side, usd in conn.execute(
            "SELECT chain, wallet, token, side, SUM(usd) FROM wallet_trades GROUP BY 1,2,3,4"):
        flows[(chain, wallet, token)][0 if side == "buy" else 1] += usd or 0
    per: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    tokens: dict[tuple, int] = defaultdict(int)
    for (chain, wallet, _), (b, s) in flows.items():
        tokens[(chain, wallet)] += 1
        if b > 0 and s > 0:
            per[(chain, wallet)].append((min(s / b - 1, 10.0), s - b))
    followed = 0
    conn.execute("DELETE FROM wallet_scores")
    for key, n_tok in tokens.items():
        rts = per.get(key, [])
        s = _summary([r for r, _ in rts])
        pnl = sum(p for _, p in rts)
        ok = s["n"] >= min_closed and (s["t_stat"] or 0) >= min_t and pnl > 0
        followed += ok
        conn.execute("INSERT INTO wallet_scores VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (key[0], key[1], now.isoformat(), n_tok, s["n"], round(pnl, 2), s["ev"], s["t_stat"],
                      s["hit_rate"], int(ok)))
    best = conn.execute("SELECT MAX(closed) FROM wallet_scores").fetchone()[0] or 0
    return {"wallets": len(tokens), "followed": followed, "max_round_trips_seen": best,
            "trades": conn.execute("SELECT COUNT(*) FROM wallet_trades").fetchone()[0]}


def smart_money_signals(conn: sqlite3.Connection, now: datetime, *, delay_min: float = 2,
                        lookback_min: float = 60) -> list[dict[str, Any]]:
    since, until = (now - timedelta(minutes=lookback_min)).isoformat(), (now - timedelta(minutes=delay_min)).isoformat()
    out = []
    for chain, token, wallet, price in conn.execute(
            "SELECT t.chain, t.token, t.wallet, t.price FROM wallet_trades t JOIN wallet_scores w "
            "ON w.chain=t.chain AND w.wallet=t.wallet AND w.followed=1 WHERE t.side='buy' AND t.at>=? AND t.at<=?",
            (since, until)):
        if not price:
            continue
        out.append({"strategy": "smart_money", "key": f"sm:{chain}:{token}", "title": f"follow {wallet[:6]}… {token[:8]}…",
                    "kind": "copy_buy", "cost": price, "locked_payoff": None, "edge": None,
                    "legs": [{"venue": chain, "market_id": token, "side": "token", "price": price}],
                    "meta": {"wallet": wallet}, "expires_at": (now + timedelta(hours=24)).isoformat()})
    return out


def wallet_exited(conn: sqlite3.Connection, chain: str, token: str, wallet: str, since: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM wallet_trades WHERE chain=? AND token=? AND wallet=? AND side='sell' "
                             "AND at>=? LIMIT 1", (chain, token, wallet, since)).fetchone())


# ----------------------------------------------------------------- 5. memecoin catalyst

def wash_score(volume_24h: float | None, liquidity: float | None, buys: int, sells: int) -> dict[str, Any]:
    """Turnover far above pool depth with balanced, high-count txns is the wash-trading fingerprint."""
    turnover = (volume_24h or 0) / liquidity if liquidity else None
    tx = buys + sells
    balance = 1 - abs(buys - sells) / tx if tx else None
    suspect = bool(turnover is not None and (turnover > 40 or (turnover > 15 and (balance or 0) > 0.95)))
    return {"turnover": None if turnover is None else round(turnover, 1), "balance": balance, "suspect": suspect}


def catalyst_screen(c: Any, conn: sqlite3.Connection | None, now: datetime,
                    report: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Passing safety screens is not a buy signal. A memecoin/mid-cap entry also needs:

    * no wash-trading fingerprint and no bundled launch (RugCheck insider graph);
    * a healthy buy/sell flow (1.1-3x buys; one-sided 5x+ is usually bots);
    * volume accelerating (last-hour rate > 1.5x the 24h hourly average);
    * >= 2 catalysts among: followed smart-money wallets buying, a credible X narrative that is
      accelerating and names the token, rising unique buyers (h1 buys > 1.3x h24 hourly rate),
      or a second-leg breakout on an established token (> 3 days, > $500k liquidity, h6 and h24 up).
    """
    buys, sells = int(getattr(c, "buys_h1", 0) or 0), int(getattr(c, "sells_h1", 0) or 0)
    w = wash_score(c.volume_24h, c.liquidity, buys, sells)
    ratio = buys / sells if sells else (float(buys) if buys else 0.0)
    vh1 = _f(getattr(c, "volume_h1", None))
    accel = (vh1 / (c.volume_24h / 24)) if vh1 and c.volume_24h else None
    bh24 = int(getattr(c, "buys_h24", 0) or 0)
    buyer_growth = buys / (bh24 / 24) if bh24 else None
    bundled = False
    if isinstance(report, dict):
        bundled = bool((_f(report.get("graphInsidersDetected")) or 0) > 0) or any(
            "bundle" in str((r or {}).get("name", "")).lower() for r in report.get("risks") or [] if isinstance(r, dict))
    smart = narrative = 0
    sym = (c.symbol or "").upper()
    if conn is not None:
        try:
            smart = conn.execute("SELECT COUNT(DISTINCT t.wallet) FROM wallet_trades t JOIN wallet_scores s ON "
                                 "s.chain=t.chain AND s.wallet=t.wallet AND s.followed=1 WHERE t.token=? AND "
                                 "t.side='buy' AND t.at>=?", (c.address, (now - timedelta(hours=6)).isoformat())
                                 ).fetchone()[0]
        except sqlite3.Error:
            smart = 0
        try:
            for tokens, phase, authors in conn.execute(
                    "SELECT tokens, phase, authors FROM x_narratives WHERE updated_at>=?",
                    ((now - timedelta(hours=6)).isoformat(),)):
                if phase in ("birth", "accelerating") and (authors or 0) >= 3 and sym and sym in str(tokens).upper():
                    narrative = 1
        except sqlite3.Error:
            narrative = 0
    second_leg = bool((c.age_hours or 0) >= 72 and (c.liquidity or 0) >= 500_000 and (c.h6 or 0) > 0 and (c.h24 or 0) > 0)
    catalysts = {"smart_money_buying": smart > 0, "x_narrative_accelerating": bool(narrative),
                 "unique_buyers_rising": bool(buyer_growth and buyer_growth > 1.3),
                 "second_leg_established": second_leg}
    n_cat = sum(catalysts.values())

    def s(ok: bool, value: Any, rule: str) -> dict[str, Any]:
        return {"pass": bool(ok), "value": value, "rule": rule}

    return {"no_wash_trading": s(not w["suspect"], w, "24h turnover <= 40x liquidity (15x if txns perfectly balanced)"),
            "not_bundled_launch": s(not bundled, bundled, "no RugCheck insider/bundle graph"),
            "buy_sell_flow": s(1.1 <= ratio <= 3.0, round(ratio, 2), "h1 buys/sells in 1.1-3.0"),
            "volume_accelerating": s(accel is not None and accel >= 1.5, None if accel is None else round(accel, 2),
                                     "h1 volume >= 1.5x 24h hourly average"),
            "catalyst": s(n_cat >= 2, {k: v for k, v in catalysts.items()}, ">= 2 independent catalysts")}


# -------------------------------------------------------------------- majors momentum

def regime_signal(closes: list[float]) -> dict[str, Any] | None:
    if len(closes) < 21:
        return None
    sma = sum(closes[-20:]) / 20
    mom = closes[-1] / closes[-8] - 1
    return {"close": closes[-1], "sma20": sma, "mom7": mom,
            "regime": "risk_on" if closes[-1] > sma and mom > 0 else "flat"}


def backtest_momentum(closes: list[float], *, cost: float = 0.003) -> dict[str, Any]:
    """Daily: in the market next day only when close > SMA20 and 7d momentum > 0. Cost per switch."""
    rets, held, bh, pos, held_flags = [], 0, [], 0, []
    for t in range(21, len(closes) - 1):
        sig = regime_signal(closes[: t + 1])
        want = 1 if sig and sig["regime"] == "risk_on" else 0
        r = closes[t + 1] / closes[t] - 1
        bh.append(r)
        x = want * r - (cost if want != pos else 0)
        pos = want
        if want:
            held += 1
        rets.append(x)
        held_flags.append(want)
    s = _summary(rets)
    s["hit_rate"] = _summary([x for x, w in zip(rets, held_flags, strict=True) if w])["hit_rate"]
    tot = math.prod(1 + x for x in rets) - 1 if rets else None
    return {**s, "days": len(rets), "days_in_market": held, "total_return": None if tot is None else round(tot, 4),
            "buy_hold_return": round(math.prod(1 + x for x in bh) - 1, 4) if bh else None,
            "sharpe_annual": round((s["sharpe"] or 0) * math.sqrt(365), 2) if s["sharpe"] else None}


def _coinbase_closes(client: Any, product: str) -> list[float]:
    r = client.get(f"{CB}/products/{product}/candles", params={"granularity": 86400})
    rows = r.json() if r.status_code == 200 else []
    rows = sorted((x for x in rows if isinstance(x, list) and len(x) >= 5), key=lambda x: x[0])
    return [float(x[4]) for x in rows]


# ------------------------------------------------------------------- book & settlement

def paper_bankroll() -> float:
    return _env("NOEMA_STRATEGY_PAPER_BANKROLL_USD", 1000.0)


def size_for(conn: sqlite3.Connection, strategy: str, sig: dict[str, Any]) -> float:
    """Proven-only: unproven strategies get a tiny probe (or shadow), proven ones get allocator
    weight x bankroll, capped at 3% per trade and $25 on top-of-book arbs. Benched -> 0."""
    row = conn.execute("SELECT weight, status FROM strategy_alloc WHERE strategy=?", (strategy,)).fetchone()
    weight, status = (row if row else (1 / len(STRATEGIES), "exploring"))
    if status == "benched":
        return 0.0
    from .proven_sizing import strategy_proof
    tier = strategy_proof(conn).get(strategy, {}).get("tier", "probe")
    if tier in ("shadow", "benched"):
        return 0.0
    if tier != "proven":  # unproven: tiny probe so evidence keeps accruing, never real size
        return round(min(_env("NOEMA_UNPROVEN_PROBE_USD", 2.5), 0.0025 * paper_bankroll()), 2)
    cap = 0.03 * paper_bankroll()
    size = min(cap, max(0.005 * paper_bankroll(), weight * paper_bankroll() * 0.10))
    if strategy == "pm_arb":
        size = min(size, 25.0)
    return round(size, 2)


def open_signal(conn: sqlite3.Connection, sig: dict[str, Any], now: datetime) -> int | None:
    from .accuracy_ledger import record_forecast

    if conn.execute("SELECT 1 FROM strategy_signals WHERE strategy=? AND key=? AND status='open'",
                    (sig["strategy"], sig["key"])).fetchone():
        return None
    size = size_for(conn, sig["strategy"], sig)
    if sig.get("edge") is not None and not clears_fee_buffer(sig.get("edge")):
        size = 0.0  # fee-plus-buffer gate: record as shadow, never enter
    status = "open" if size > 0 else "shadow"
    if status == "shadow" and conn.execute("SELECT 1 FROM strategy_signals WHERE strategy=? AND key=? AND "
                                           "status='shadow' AND at>=?", (sig["strategy"], sig["key"],
                                           (now - timedelta(hours=24)).isoformat())).fetchone():
        return None
    leg = sig["legs"][0]
    fid = None
    if leg["side"] in ("yes", "no"):
        yes_price = leg["price"] if leg["side"] == "yes" else 1 - leg["price"]
        fair = sig.get("fair") or (1.0 if sig.get("locked_payoff") else None) or leg["price"]
        p_yes = fair if leg["side"] == "yes" else 1 - fair
        fid = record_forecast(conn, agent_id=f"strategy:{sig['strategy']}", venue=leg["venue"],
                              market_id=leg["market_id"], predicted_probability=round(min(0.999, max(0.001, p_yes)), 4),
                              market_price=round(yes_price, 4), recorded_at=now,
                              decision="paper_trade" if status == "open" else "shadow",
                              reasoning={"key": sig["key"], "kind": sig.get("kind"), "edge": sig.get("edge")},
                              horizon="to_resolution", metadata={"strategy": sig["strategy"], "kind": sig.get("kind")})
    cur = conn.execute(
        "INSERT INTO strategy_signals (at, strategy, key, title, legs_json, cost, locked_payoff, edge, size_usd, "
        "reason, meta_json, forecast_id, expires_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), sig["strategy"], sig["key"], sig.get("title"), json.dumps(sig["legs"]), sig["cost"],
         sig.get("locked_payoff"), sig.get("edge"), size, sig.get("kind"),
         json.dumps({k: v for k, v in sig.items() if k in ("meta", "similarity", "needs_resolution_review")}),
         fid, sig.get("expires_at"), status))
    return int(cur.lastrowid) if status == "open" else None


def _leg_value(leg: dict[str, Any], outcome: int | None, quote: dict[str, Any] | None) -> float | None:
    if outcome is not None:
        return float(outcome if leg["side"] == "yes" else 1 - outcome)
    if quote:
        return quote["yes_bid"] if leg["side"] == "yes" else quote["no_bid"]
    return None


def settle(conn: sqlite3.Connection, quotes: list[dict[str, Any]], now: datetime,
           prices: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Binary legs settle on official outcomes; expired positions exit at the current bid (the
    prior entry cost already charged fees). Token legs exit at the latest price."""
    by = {(q["venue"], q["market_id"]): q for q in quotes}
    prices = prices or {}
    closed = []
    for sid, strategy, legs_json, cost, size, expires, at, meta in conn.execute(
            "SELECT id, strategy, legs_json, cost, size_usd, expires_at, at, meta_json FROM strategy_signals "
            "WHERE status='open'").fetchall():
        legs = json.loads(legs_json)
        expired = bool(expires and _ts(expires) and _ts(expires) <= now)
        if legs[0]["side"] == "token":
            px = prices.get(legs[0]["market_id"])
            wallet = (json.loads(meta or "{}").get("meta") or {}).get("wallet")
            gone = bool(wallet and wallet_exited(conn, legs[0]["venue"], legs[0]["market_id"], wallet, at))
            if px and (expired or gone):
                ret = px * (1 - 0.006) / (cost * (1 + 0.006)) - 1  # 30bps fee + 30bps slip each way
                pnl = size * ret
                conn.execute("UPDATE strategy_signals SET status='closed', closed_at=?, exit_value=?, pnl_usd=? "
                             "WHERE id=?", (now.isoformat(), px, round(pnl, 4), sid))
                closed.append({"id": sid, "strategy": strategy, "pnl_usd": round(pnl, 4)})
            continue
        values, final = [], True
        for leg in legs:
            o = conn.execute("SELECT outcome_yes FROM outcomes WHERE venue=? AND market_id=?",
                             (leg["venue"], leg["market_id"])).fetchone()
            if o is None:
                final = False
            values.append(_leg_value(leg, o[0] if o else None, by.get((leg["venue"], leg["market_id"]))))
        if not final and not expired:
            continue
        if any(v is None for v in values):
            continue
        payoff = sum(values) - (0 if final else len(legs) * _env("NOEMA_STRATEGY_SLIPPAGE", 0.005))
        pnl = size * (payoff / cost - 1)
        conn.execute("UPDATE strategy_signals SET status='closed', closed_at=?, exit_value=?, pnl_usd=? WHERE id=?",
                     (now.isoformat(), round(payoff, 4), round(pnl, 4), sid))
        closed.append({"id": sid, "strategy": strategy, "pnl_usd": round(pnl, 4)})
    return closed


# ------------------------------------------------------------------------ 6. allocator

def meme_catalyst_closed(conn: sqlite3.Connection) -> list[tuple[float, float]]:
    try:
        rows = conn.execute("SELECT realized_usd, exits_json FROM alpha_positions WHERE status='closed' "
                            "AND exits_json LIKE '%catalyst_v1%'").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for realized, ex in rows:
        cost = _f((json.loads(ex or "{}")).get("entry_cost_usd"))
        if cost:
            out.append((float(realized or 0), cost))
    return out


def allocate(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Weights by shrunk per-trade return (n/(n+K) x mean / sd). Exploration floor for n < 30.
    Benched: n >= 30 and shrunk mean < 0, or a backtest that found no edge after fees."""
    bts = {r[0]: r[1] for r in conn.execute("SELECT strategy, verdict FROM strategy_backtests")}
    raw, rows = {}, {}
    for st in STRATEGIES:
        pairs = conn.execute("SELECT pnl_usd, size_usd FROM strategy_signals WHERE strategy=? "
                             "AND status='closed' AND size_usd>0", (st,)).fetchall()
        if st == "meme_catalyst":  # entries live in the alpha book once they pass the catalyst gate
            pairs = list(pairs) + meme_catalyst_closed(conn)
        rets = [p / sz for p, sz in pairs]
        s = _summary(rets)
        pnl = sum(p for p, _ in pairs)
        n = s["n"]
        shrink = n / (n + SHRINK_K)
        sh = (s["sharpe"] or 0) * shrink
        status, reason = "exploring", f"{n} settled < {MIN_BENCH_N}"
        if bts.get(st) in ("no_edge_after_fees",) and n < MIN_BENCH_N:
            status, reason = "benched", "backtest: no edge after fees"
        elif n >= MIN_BENCH_N:
            status, reason = (("active", "positive shrunk EV") if (s["ev"] or 0) * shrink > 0
                              else ("benched", "negative shrunk EV"))
        raw[st] = 0.0 if status == "benched" else max(0.02, sh) if status == "active" else 0.02
        rows[st] = {"strategy": st, "status": status, "reason": reason, "n": n, "ev": s["ev"],
                    "sharpe": s["sharpe"], "pnl_usd": round(pnl, 2)}
    tot = sum(raw.values()) or 1.0
    out = []
    for st, r in rows.items():
        r["weight"] = round(raw[st] / tot, 4)
        conn.execute("INSERT OR REPLACE INTO strategy_alloc VALUES (?,?,?,?,?,?,?,?,?)",
                     (st, now.isoformat(), r["weight"], r["status"], r["reason"], r["n"], r["ev"], r["sharpe"],
                      r["pnl_usd"]))
        out.append(r)
    return out


def _store_bt(conn: sqlite3.Connection, strategy: str, now: datetime, res: dict[str, Any]) -> None:
    conn.execute("INSERT OR REPLACE INTO strategy_backtests VALUES (?,?,?,?,?,?,?,?)",
                 (strategy, now.isoformat(), res.get("n"), res.get("ev"), res.get("hit_rate"), res.get("sharpe"),
                  res.get("verdict"), json.dumps(res, default=str)))


# ----------------------------------------------------------------------------- cycle

def run_cycle(conn: sqlite3.Connection, *, client: Any = None, now: datetime | None = None,
              fetch: bool = True, backtest: bool = True) -> dict[str, Any]:
    ensure_schema(conn)
    now = now or datetime.now(UTC)
    quotes = _quotes(conn, now)
    report: dict[str, Any] = {"quotes": len(quotes), "signals": {}, "opened": {}, "notes": {}}
    rows = history_rows(conn)
    if backtest:
        bt = backtest_fav_longshot(rows)
        _store_bt(conn, "fav_longshot", now, bt)
        for st, verdict in (("pm_arb", "forward_only_no_book_history"), ("news_latency", "forward_only"),
                            ("smart_money", "forward_only"), ("meme_catalyst", "forward_only")):
            if not conn.execute("SELECT 1 FROM strategy_backtests WHERE strategy=?", (st,)).fetchone():
                _store_bt(conn, st, now, {"verdict": verdict, "n": 0})
    bt_row = conn.execute("SELECT detail_json FROM strategy_backtests WHERE strategy='fav_longshot'").fetchone()
    bt = json.loads(bt_row[0]) if bt_row else {}
    allocate(conn, now)
    sigs = ladder_arbs(quotes) + cross_venue_gaps(quotes)
    try:
        from . import xv_match
        sigs += xv_match.arb_signals(xv_match.stored_pairs(conn))
    except sqlite3.Error:
        pass
    sigs += news_latency_signals(conn, quotes, now)
    sigs += fav_longshot_signals(quotes, bt, rows, now)
    prices: dict[str, float] = {}
    conn.commit()  # never hold the shared DB write lock across network calls
    if fetch:
        import httpx
        own = client is None
        c = client or httpx.Client(timeout=10, headers={"accept": "application/json"})
        try:
            report["notes"]["smart_money"] = _smart_money_fetch(conn, c, now, prices)
            report["notes"]["majors"] = _majors(conn, c, now, sigs, backtest, prices)
        finally:
            if own:
                c.close()
    report["notes"]["smart_money_scores"] = score_wallets(conn, now)
    allocate(conn, now)
    sigs += smart_money_signals(conn, now)
    for s in sigs:
        report["signals"][s["strategy"]] = report["signals"].get(s["strategy"], 0) + 1
        if open_signal(conn, s, now):
            report["opened"][s["strategy"]] = report["opened"].get(s["strategy"], 0) + 1
    report["closed"] = settle(conn, quotes, now, prices)
    report["alloc"] = allocate(conn, now)
    conn.commit()
    return report


def _smart_money_fetch(conn: sqlite3.Connection, c: Any, now: datetime, prices: dict[str, float],
                       max_tokens: int = 6, pause: float = 2.2) -> dict[str, Any]:
    """Public GeckoTerminal pool trades for the top trending tokens (~2 calls each, <30/min)."""
    rows = conn.execute("SELECT entity FROM culture_signals WHERE kind='token' AND updated_at>=? ORDER BY score DESC "
                        "LIMIT ?", ((now - timedelta(hours=3)).isoformat(), max_tokens)).fetchall()
    rows += conn.execute("SELECT legs_json FROM strategy_signals WHERE strategy='smart_money' AND status='open'"
                         ).fetchall()
    added = 0
    errors = 0
    for (entity,) in rows:
        if entity.startswith("["):
            leg = json.loads(entity)[0]
            chain, addr = leg["venue"], leg["market_id"]
        else:
            chain, _, addr = entity.partition(":")
        net = {"solana": "solana", "ethereum": "eth", "base": "base", "bsc": "bsc"}.get(chain)
        if not net or not addr:
            continue
        try:
            r = c.get(f"{GT}/networks/{net}/tokens/{addr}/pools", params={"page": 1})
            time.sleep(pause)
            data = (r.json().get("data") or []) if r.status_code == 200 else []
            if not data:
                continue
            at = data[0].get("attributes") or {}
            px = _f(at.get("base_token_price_usd"))
            if px:
                prices[addr] = px
            tr = c.get(f"{GT}/networks/{net}/pools/{at.get('address')}/trades")
            time.sleep(pause)
            added += ingest_trades(conn, chain, addr, (tr.json().get("data") or []) if tr.status_code == 200 else [])
            conn.commit()
        except Exception:  # noqa: BLE001 - one bad token never stops the cycle
            errors += 1
    conn.execute("DELETE FROM wallet_trades WHERE at < ?", ((now - timedelta(days=30)).isoformat(),))
    return {"tokens": len(rows), "new_trades": added, "errors": errors}


def _majors(conn: sqlite3.Connection, c: Any, now: datetime, sigs: list[dict[str, Any]],
            backtest: bool, prices: dict[str, float]) -> dict[str, Any]:
    out = {}
    agg = []
    for sym in ("BTC", "ETH", "SOL"):
        try:
            closes = _coinbase_closes(c, f"{sym}-USD")
        except Exception:  # noqa: BLE001
            closes = []
        conn.commit()
        sig = regime_signal(closes)
        if not sig:
            out[sym] = "no_data"
            continue
        if backtest:
            b = backtest_momentum(closes)
            out[sym] = {"regime": sig["regime"], "backtest": b}
            agg.append(b)
        conn.execute("INSERT OR REPLACE INTO majors_state VALUES (?,?,?,?,?,?)",
                     (sym, now.isoformat(), sig["close"], sig["sma20"], sig["mom7"], sig["regime"]))
        if sig["regime"] == "risk_on":
            sigs.append({"strategy": "majors_momentum", "key": f"mm:{sym}", "title": f"{sym} trend sleeve (>SMA20, 7d mom +)",
                         "kind": "risk_on", "cost": sig["close"], "locked_payoff": None, "edge": None,
                         "legs": [{"venue": "coinbase", "market_id": sym, "side": "token", "price": sig["close"]}],
                         "expires_at": (now + timedelta(hours=24)).isoformat()})
        prices[sym] = sig["close"]
    if agg and backtest:
        n = sum(b["n"] for b in agg)
        ev = sum((b["ev"] or 0) * b["n"] for b in agg) / n if n else None
        hit = sum((b["hit_rate"] or 0) * b["n"] for b in agg) / n if n else None
        verdict = "positive_backtest" if ev and ev > 0 else "no_edge_after_fees"
        _store_bt(conn, "majors_momentum", now, {"n": n, "ev": None if ev is None else round(ev, 5),
                                                  "hit_rate": None if hit is None else round(hit, 3),
                                                  "sharpe": None, "verdict": verdict, "per_symbol": out,
                                                  "unit": "daily return incl. 30bps per switch"})
    return out


# ------------------------------------------------------------------------------ board

def strategies_board(db_path: str) -> dict[str, Any]:
    """Read-only payload for /api/strategies and the STRATEGIES panel."""
    out: dict[str, Any] = {"paper": True, "live_supported": LIVE_SUPPORTED, "strategies": [], "recent": []}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        alloc = {r[0]: r for r in conn.execute("SELECT strategy, weight, status, reason, n, ev, sharpe, pnl_usd "
                                               "FROM strategy_alloc")}
        bts = {r[0]: r for r in conn.execute("SELECT strategy, n, ev, hit_rate, verdict FROM strategy_backtests")}
        for st in STRATEGIES:
            a, b = alloc.get(st), bts.get(st)
            opn = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_usd),0) FROM strategy_signals WHERE strategy=? "
                               "AND status='open'", (st,)).fetchone()
            out["strategies"].append({
                "strategy": st, "label": LABELS[st], "status": a[2] if a else "exploring",
                "reason": a[3] if a else None, "weight": a[1] if a else None, "n": a[4] if a else 0,
                "ev": a[5] if a else None, "sharpe": a[6] if a else None, "pnl_usd": a[7] if a else 0.0,
                "open": opn[0], "open_usd": round(opn[1], 2),
                "backtest": {"n": b[1], "ev": b[2], "hit_rate": b[3], "verdict": b[4]} if b else None})
        for row in out["strategies"]:
            row["shadow_24h"] = conn.execute("SELECT COUNT(*) FROM strategy_signals WHERE strategy=? AND "
                                             "status='shadow' AND at>=datetime('now','-1 day')",
                                             (row["strategy"],)).fetchone()[0]
        try:
            from .proven_sizing import strategy_proof
            proof = strategy_proof(conn)
            for row in out["strategies"]:
                p = proof.get(row["strategy"], {})
                row["sizing_tier"], row["sizing_why"] = p.get("tier"), p.get("why")
        except sqlite3.Error:
            pass
        out["recent"] = [{"at": r[0], "strategy": r[1], "title": r[2], "edge": r[3], "size_usd": r[4], "status": r[5],
                          "pnl_usd": r[6]} for r in conn.execute(
            "SELECT at, strategy, title, edge, size_usd, status, pnl_usd FROM strategy_signals ORDER BY id DESC LIMIT 20")]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def main() -> None:  # pragma: no cover - process entry
    import argparse

    ap = argparse.ArgumentParser(description="TEV structural-edge strategies (paper only)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=_env("NOEMA_STRATEGY_INTERVAL_SECONDS", 300))
    args = ap.parse_args()
    db = os.getenv("NOEMA_DB_PATH", "data/noema.db")
    last_bt = 0.0
    while True:
        conn = sqlite3.connect(db, timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            heavy = time.time() - last_bt > 3600
            rep = run_cycle(conn, backtest=heavy)
            if heavy:
                last_bt = time.time()
            print(json.dumps({"at": datetime.now(UTC).isoformat(), "signals": rep["signals"], "opened": rep["opened"],
                              "closed": len(rep["closed"]), "quotes": rep["quotes"]}), flush=True)
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad cycle
            print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:200]}), flush=True)
        finally:
            conn.close()
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
