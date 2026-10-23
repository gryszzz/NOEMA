"""X edge engine: turns the X wire into scored, ledgered evidence.

Pieces (all evidence only; nothing here can place orders, change live flags or configuration):

1. Smart-money ledger  - per author: did posts precede correct moves (token 1h/24h, market price
   move or resolution)? Brier / hit rate with n/(n+prior) shrinkage; promote early-and-right
   accounts, demote shillers, and plan ``from:`` queries for the top accounts.
2. Stance              - see ``x_stance``: LLM stance toward each matched market's YES outcome.
3. Bursts              - velocity z-scores per entity (X counts API + wire sample) compared with
   the market price since the burst began -> "not priced in yet" alerts.
4. Narratives          - TF-IDF leader clustering into narratives with a lifecycle
   (birth, accelerating, peak, decaying) and crypto sector rotation.
5. Manipulation shield - copy-paste rings, new-account swarms, synchronized cashtags, paid-KOL
   patterns, homoglyph obfuscation -> "avoid, being shilled" warnings.
6. KOL -> wallet check - contract addresses go to the alpha screens (culture_signals) and
   GeckoTerminal pool trades are checked for early buyers dumping into the hype.
7. Counter-signal      - one-sided euphoria / panic as a contrarian hypothesis.
8. Every hypothesis is a separate shadow agent in the accuracy ledger (decision 'observe'), so the
   accuracy engine weights or benches each one on resolved outcomes.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .news_crawler import STOP, sanitize
from .sqlite_util import connect as sqlite_connect

AGENTS = {"stance": "x_social", "smart": "x_smart_money", "burst": "x_burst",
          "contra": "x_contrarian", "shield": "x_shield"}
PRIOR_N = 10.0
MAX_NUDGE = 0.06
DEDUP_HOURS = 6
MAJORS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
DEFAULT_PLAN = "/workspace/noema-run/x-plan.json"
GT = "https://api.geckoterminal.com/api/v2"
CB = "https://api.exchange.coinbase.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS x_author_calls (post_id TEXT NOT NULL, author TEXT NOT NULL, venue TEXT NOT NULL,
    target TEXT NOT NULL, direction INTEGER NOT NULL, confidence REAL, t0 TEXT NOT NULL, p0 REAL,
    p1h REAL, p24h REAL, outcome_1h INTEGER, outcome_24h INTEGER, resolved_outcome INTEGER, early INTEGER,
    done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (post_id, venue, target));
CREATE TABLE IF NOT EXISTS x_author_scores (author TEXT PRIMARY KEY, n INTEGER, hits INTEGER, brier REAL,
    skill REAL, shrunk REAL, early_share REAL, posts INTEGER, shill_posts INTEGER, shill_rate REAL,
    status TEXT, topics TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS x_counts (entity TEXT NOT NULL, start TEXT NOT NULL, count INTEGER NOT NULL,
    topic TEXT, query TEXT, venue TEXT, market_id TEXT, fetched_at TEXT, PRIMARY KEY (entity, start));
CREATE TABLE IF NOT EXISTS x_bursts (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, entity TEXT NOT NULL,
    source TEXT, z REAL, rate REAL, baseline REAL, began_at TEXT, direction REAL, venue TEXT, market_id TEXT,
    title TEXT, price_then REAL, price_now REAL, price_move REAL, status TEXT);
CREATE TABLE IF NOT EXISTS x_narratives (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, terms TEXT,
    sector TEXT, tokens TEXT, first_seen TEXT, last_seen TEXT, posts INTEGER, authors INTEGER,
    rate_now REAL, rate_prev REAL, peak_rate REAL, phase TEXT, stance REAL, sample TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS x_shill_warnings (entity TEXT PRIMARY KEY, kind TEXT, score REAL, reasons TEXT,
    authors INTEGER, posts INTEGER, first_at TEXT, updated_at TEXT, sample TEXT);
CREATE TABLE IF NOT EXISTS x_contracts (chain TEXT NOT NULL, address TEXT NOT NULL, symbol TEXT,
    first_at TEXT, mentions INTEGER, authors INTEGER, kol_authors INTEGER, shill_score REAL, pool TEXT,
    price REAL, liquidity REAL, dump_json TEXT, verdict TEXT, checked_at TEXT, pushed INTEGER DEFAULT 0,
    PRIMARY KEY (chain, address));
CREATE TABLE IF NOT EXISTS x_crowd (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, venue TEXT,
    target TEXT, title TEXT, n INTEGER, mean_stance REAL, one_side REAL, signal TEXT);
CREATE TABLE IF NOT EXISTS x_edge_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, detail TEXT);
"""


def ensure_edge_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Prices:
    """Point-in-time prices. Markets: forecast_ledger snapshot mids. Tokens: Coinbase (majors) or
    GeckoTerminal candles, fetched read-only and cached per run. ``fetch=False`` keeps tests offline."""

    def __init__(self, conn: sqlite3.Connection, *, fetch: bool = True) -> None:
        self.conn, self.fetch = conn, fetch
        self.candles: dict[str, list[tuple[float, float]]] = {}
        self.calls = 0

    def market(self, venue: str, mid: str, at: datetime, *, after: bool = False) -> float | None:
        if not _has(self.conn, "forecast_ledger"):
            return None
        t = at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        q = ("SELECT snapshot_json FROM forecast_ledger WHERE venue=? AND market_id=? AND created_at>=? "
             "ORDER BY created_at ASC LIMIT 1") if after else (
            "SELECT snapshot_json FROM forecast_ledger WHERE venue=? AND market_id=? AND created_at<=? "
            "ORDER BY created_at DESC LIMIT 1")
        row = self.conn.execute(q, (venue, mid, t)).fetchone()
        if not row:
            return None
        try:
            s = json.loads(row[0]) or {}
            bid, ask = s.get("yes_bid"), s.get("yes_ask")
            if bid is None or ask is None:
                return None
            return round((float(bid) + float(ask)) / 2, 4)
        except (TypeError, ValueError):
            return None

    def latest_market(self, venue: str, mid: str) -> float | None:
        return self.market(venue, mid, datetime.now(UTC) + timedelta(days=1))

    def _series(self, key: str) -> list[tuple[float, float]]:
        if key in self.candles or not self.fetch:
            return self.candles.get(key, [])
        rows: list[tuple[float, float]] = []
        try:
            import httpx
            with httpx.Client(timeout=8, headers={"accept": "application/json"}) as c:
                if key in MAJORS:
                    end = datetime.now(UTC)
                    for back in (0, 1):  # two windows of 300 x 5m candles (~50h)
                        e = end - timedelta(minutes=1500 * back)
                        r = c.get(f"{CB}/products/{MAJORS[key]}/candles", params={
                            "granularity": 300, "start": (e - timedelta(minutes=1500)).isoformat(),
                            "end": e.isoformat()})
                        self.calls += 1
                        if r.status_code == 200:
                            rows += [(float(x[0]), float(x[4])) for x in r.json()]
                else:
                    chain, _, addr = key.partition(":")
                    pools = c.get(f"{GT}/networks/{chain}/tokens/{addr}/pools", params={"page": 1}).json()
                    self.calls += 1
                    pool = ((pools.get("data") or [{}])[0].get("attributes") or {}).get("address")
                    if pool:
                        d = c.get(f"{GT}/networks/{chain}/pools/{pool}/ohlcv/minute",
                                  params={"aggregate": 15, "limit": 200, "token": addr}).json()
                        self.calls += 1
                        rows = [(float(x[0]), float(x[4])) for x in
                                ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []]
        except Exception:  # noqa: BLE001 - missing prices just leave calls unresolved
            rows = []
        self.candles[key] = sorted(set(rows))
        return self.candles[key]

    def token(self, key: str, at: datetime, *, after: bool = False, tol_h: float = 1.0) -> float | None:
        s = self._series(key)
        if not s:
            return None
        ts = at.timestamp()
        best = None
        for t, px in s:
            if after and t >= ts:
                best = (t, px)
                break
            if not after and t <= ts:
                best = (t, px)
        if best and abs(best[0] - ts) <= tol_h * 3600 and best[1] > 0:
            return best[1]
        return None


def _stance_rows(conn: sqlite3.Connection, since: datetime, *, min_conf: float = 0.0) -> list[tuple]:
    return conn.execute(
        "SELECT s.post_id, s.venue, s.market_id, s.stance, s.confidence, s.source, p.author_handle, p.created_at, "
        "p.pump_risk, p.author_followers, l.title FROM x_stance s JOIN x_posts p USING (post_id) "
        "LEFT JOIN x_market_links l ON l.post_id=s.post_id AND l.venue=s.venue AND l.market_id=s.market_id "
        "WHERE s.relevant=1 AND s.confidence>=? AND p.created_at>=?",
        (min_conf, since.isoformat())).fetchall()


def _token_key(venue: str, mid: str) -> str | None:
    return mid if venue == "token" and mid in MAJORS else None


def _recent_forecast(conn: sqlite3.Connection, agent: str, venue: str, mid: str, now: datetime) -> bool:
    if not _has(conn, "accuracy_forecasts"):
        return False
    return bool(conn.execute("SELECT 1 FROM accuracy_forecasts WHERE agent_id=? AND venue=? AND market_id=? AND "
                             "recorded_at>=? LIMIT 1",
                             (agent, venue, mid, (now - timedelta(hours=DEDUP_HOURS)).isoformat())).fetchone())


def record(conn: sqlite3.Connection, agent: str, venue: str, mid: str, p: float, price: float, *,
           reasoning: dict[str, Any], confidence: float, now: datetime, token: bool = False) -> int:
    """Shadow forecast (decision 'observe') so the accuracy engine scores the hypothesis."""
    from .accuracy_ledger import record_forecast
    if os.getenv("NOEMA_X_LEDGER_ENABLED", "1") != "1" or _recent_forecast(conn, agent, venue, mid, now):
        return 0
    meta: dict[str, Any] = {"signal": agent, "role": "evidence_only_never_sole_trigger"}
    if token:
        meta["kind"] = "token_up"
    record_forecast(conn, agent_id=agent, venue=venue, market_id=mid,
                    predicted_probability=round(min(0.99, max(0.01, p)), 4), market_price=price,
                    decision="observe", horizon="1h" if token else "resolution", recorded_at=now,
                    confidence=round(min(1.0, max(0.0, confidence)), 3),
                    reasoning={**reasoning, "note": "X evidence only; never a sole trade trigger"},
                    metadata=meta)
    return 1


# ======================================================================= 1. smart money

def collect_calls(conn: sqlite3.Connection, now: datetime, prices: Prices) -> int:
    """Confident stances become author calls with an entry price."""
    n = 0
    for pid, venue, mid, stance, conf, _src, author, created, *_ in _stance_rows(
            conn, now - timedelta(days=3), min_conf=0.35):
        t0 = _ts(created)
        if abs(stance) < 0.2 or t0 is None:
            continue
        tk = _token_key(venue, mid)
        if venue == "token" and not tk:
            continue
        p0 = prices.token(tk, t0) if tk else prices.market(venue, mid, t0)
        cur = conn.execute("INSERT OR IGNORE INTO x_author_calls (post_id, author, venue, target, direction, "
                           "confidence, t0, p0) VALUES (?,?,?,?,?,?,?,?)",
                           (pid, author.lower(), venue, mid, 1 if stance > 0 else -1, conf, t0.isoformat(), p0))
        n += cur.rowcount
    return n


def _market_resolution(conn: sqlite3.Connection, venue: str, mid: str) -> int | None:
    if not _has(conn, "accuracy_resolutions"):
        return None
    row = conn.execute("SELECT r.outcome FROM accuracy_resolutions r JOIN accuracy_forecasts f ON f.id=r.forecast_id "
                       "WHERE f.venue=? AND f.market_id=? AND r.source!='later_mark' LIMIT 1", (venue, mid)).fetchone()
    return int(row[0]) if row else None


def call_hit(p0: float, px: float | None, direction: int, *, token: bool) -> int | None:
    """Token: >0.5% move in the called direction. Market: >=1pp."""
    if px is None or p0 is None:
        return None
    mv = (px - p0) / p0 if token else px - p0
    return 1 if round(direction * mv, 6) > (0.005 if token else 0.01) else 0


def resolve_calls(conn: sqlite3.Connection, now: datetime, prices: Prices) -> int:
    done = 0
    rows = conn.execute("SELECT post_id, venue, target, direction, t0, p0, p1h, p24h FROM x_author_calls "
                        "WHERE done=0").fetchall()
    for pid, venue, tgt, d, t0s, p0, p1h, p24h in rows:
        t0 = _ts(t0s)
        if t0 is None:
            continue
        tk = _token_key(venue, tgt)

        def px_at(h: int, tk: str | None = tk, t0: datetime = t0, venue: str = venue,
                  tgt: str = tgt) -> float | None:
            at = t0 + timedelta(hours=h)
            if at > now:
                return None
            return prices.token(tk, at, after=True) if tk else prices.market(venue, tgt, at, after=True)

        if p0 is None:
            p0 = prices.token(tk, t0) if tk else prices.market(venue, tgt, t0)
        if p0 is None:
            if now - t0 > timedelta(days=3):
                conn.execute("UPDATE x_author_calls SET done=1 WHERE post_id=? AND venue=? AND target=?",
                             (pid, venue, tgt))
            continue
        p1h = p1h if p1h is not None else px_at(1)
        p24h = p24h if p24h is not None else px_at(24)
        res = None if tk else _market_resolution(conn, venue, tgt)
        res_hit = None if res is None else int((res == 1) == (d > 0))
        finished = p24h is not None or res is not None or now - t0 > timedelta(days=3)
        conn.execute("UPDATE x_author_calls SET p0=?, p1h=?, p24h=?, outcome_1h=?, outcome_24h=?, resolved_outcome=?, "
                     "done=? WHERE post_id=? AND venue=? AND target=?",
                     (p0, p1h, p24h, call_hit(p0, p1h, d, token=bool(tk)), call_hit(p0, p24h, d, token=bool(tk)),
                      res_hit, int(finished), pid, venue, tgt))
        done += int(finished)
    return done


def mark_early(conn: sqlite3.Connection) -> None:
    """'Early' = among the first quarter of authors calling that target within a 6h window."""
    groups: dict[tuple[str, str], list[tuple[str, datetime]]] = defaultdict(list)
    for pid, v, t, t0 in conn.execute("SELECT post_id, venue, target, t0 FROM x_author_calls").fetchall():
        d = _ts(t0)
        if d:
            groups[(v, t)].append((pid, d))
    for (v, t), items in groups.items():
        for pid, d in items:
            window = [x for x in items if abs((x[1] - d).total_seconds()) <= 6 * 3600]
            rank = sum(1 for x in window if x[1] < d)
            conn.execute("UPDATE x_author_calls SET early=? WHERE post_id=? AND venue=? AND target=?",
                         (int(rank <= len(window) // 4), pid, v, t))


def author_score(calls: list[tuple[float, int]], *, prior: float = PRIOR_N) -> dict[str, float | int | None]:
    """calls: (confidence, hit). Implied p = 0.5 + 0.4*confidence. Skill vs a coin-flip Brier of 0.25."""
    n = len(calls)
    if not n:
        return {"n": 0, "hits": 0, "brier": None, "skill": 0.0, "shrunk": 0.0}
    brier = sum((0.5 + 0.4 * c - h) ** 2 for c, h in calls) / n
    skill = 1 - brier / 0.25
    return {"n": n, "hits": sum(h for _, h in calls), "brier": round(brier, 4), "skill": round(skill, 4),
            "shrunk": round(n / (n + prior) * skill, 4)}


def score_authors(conn: sqlite3.Connection, now: datetime, *, prior: float = PRIOR_N) -> int:
    calls: dict[str, list[tuple[float, int]]] = defaultdict(list)
    early: Counter[str] = Counter()
    for author, conf, hit, e in conn.execute(
            "SELECT author, confidence, COALESCE(resolved_outcome, outcome_24h, outcome_1h), early FROM x_author_calls "
            "WHERE COALESCE(resolved_outcome, outcome_24h, outcome_1h) IS NOT NULL").fetchall():
        calls[author].append((float(conf or 0.5), int(hit)))
        early[author] += int(e or 0)
    shill_auth = _shield_authors(conn)
    n_rows = 0
    for author, cnt, shill, topics in conn.execute(
            "SELECT LOWER(author_handle), COUNT(*), SUM(pump_risk>=0.5), GROUP_CONCAT(DISTINCT topic) FROM x_posts "
            "GROUP BY 1").fetchall():
        s = author_score(calls.get(author, []), prior=prior)
        n = int(s["n"] or 0)
        shill_n = int(shill or 0) + shill_auth.get(author, 0)
        shill_rate = min(1.0, shill_n / max(1, cnt))
        if shill_rate >= 0.3 or (n >= 5 and float(s["shrunk"] or 0) <= -0.1):
            status = "demoted"
        elif n >= 5 and float(s["shrunk"] or 0) >= 0.08 and int(s["hits"] or 0) / n >= 0.55 and shill_rate < 0.2:
            status = "promoted"
        else:
            status = "neutral"
        conn.execute("INSERT OR REPLACE INTO x_author_scores (author, n, hits, brier, skill, shrunk, early_share, posts, "
                     "shill_posts, shill_rate, status, topics, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (author, n, s["hits"], s["brier"], s["skill"], s["shrunk"],
                      round(early[author] / n, 3) if n else None, cnt, shill_n, round(shill_rate, 3), status,
                      topics, now.isoformat()))
        n_rows += 1
    return n_rows


def author_multiplier(conn: sqlite3.Connection, handle: str) -> float:
    """Credibility multiplier from the smart-money ledger (1.0 when unknown or before first run)."""
    try:
        row = conn.execute("SELECT status, shrunk FROM x_author_scores WHERE author=?", (handle.lower(),)).fetchone()
    except sqlite3.OperationalError:
        return 1.0
    if not row:
        return 1.0
    if row[0] == "demoted":
        return 0.3
    if row[0] == "promoted":
        return round(1.0 + min(1.0, 2 * float(row[1] or 0)), 3)
    return 1.0


def smart_money_forecasts(conn: sqlite3.Connection, now: datetime, prices: Prices) -> int:
    rows = conn.execute(
        "SELECT s.venue, s.market_id, s.stance, s.confidence, a.shrunk FROM x_stance s JOIN x_posts p USING (post_id) "
        "JOIN x_author_scores a ON a.author=LOWER(p.author_handle) WHERE a.status='promoted' AND s.relevant=1 "
        "AND p.created_at>=?", ((now - timedelta(hours=DEDUP_HOURS)).isoformat(),)).fetchall()
    agg: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for venue, mid, st, conf, shrunk in rows:
        w = float(shrunk) * float(conf)
        a = agg[(venue, mid)]
        a[0] += w * float(st)
        a[1] += w
        a[2] += 1
    out = 0
    for (venue, mid), (ws, w, n) in agg.items():
        if w <= 0:
            continue
        lean = ws / w
        tk = _token_key(venue, mid)
        if venue == "token" and not tk:
            continue
        price = prices.token(tk, now, tol_h=2) if tk else prices.latest_market(venue, mid)
        if price is None:
            continue
        why = {"promoted_authors": int(n), "lean": round(lean, 3)}
        if tk:
            out += record(conn, AGENTS["smart"], "x_token", f"major:{mid}", 0.5 + 0.15 * lean, price,
                          reasoning=why, confidence=min(1.0, w), now=now, token=True)
        else:
            out += record(conn, AGENTS["smart"], venue, mid, price + MAX_NUDGE * lean * min(1.0, w), price,
                          reasoning=why, confidence=min(1.0, w), now=now)
    return out


# ======================================================================= 3. bursts

def store_counts(conn: sqlite3.Connection, doc: dict[str, Any], now: datetime) -> int:
    """Persist a ``tev.x_counts.v1`` inbox doc (X recent-counts response, hourly buckets)."""
    ensure_edge_schema(conn)
    raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
    ent = sanitize(doc.get("entity") or doc.get("query") or "", 80)
    if not ent:
        return 0
    n = 0
    for b in (raw.get("data") or [])[:400]:
        if not isinstance(b, dict):
            continue
        st, c = _ts(b.get("start")), b.get("tweet_count")
        if st is None or not isinstance(c, int) or c < 0:
            continue
        conn.execute("INSERT OR REPLACE INTO x_counts (entity, start, count, topic, query, venue, market_id, fetched_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (ent, st.isoformat(), c, sanitize(doc.get("topic") or "", 20), sanitize(doc.get("query") or "", 300),
                      sanitize(doc.get("venue") or "", 40) or None, sanitize(doc.get("market_id") or "", 120) or None,
                      now.isoformat()))
        n += 1
    return n


def zscore(series: list[float], *, baseline: int = 48) -> tuple[float, float, float]:
    """z of the last value vs the prior ``baseline`` values (sd floored at Poisson sqrt(mean+1))."""
    if len(series) < 6:
        return 0.0, series[-1] if series else 0.0, 0.0
    cur, base = series[-1], series[-1 - baseline:-1]
    mu = sum(base) / len(base)
    sd = max(math.sqrt(sum((x - mu) ** 2 for x in base) / len(base)), math.sqrt(mu + 1.0))
    return round((cur - mu) / sd, 2), cur, round(mu, 2)


_ENTITY_STOP = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "president", "breaking news", "the", "this", "that", "breaking", "just", "new", "today", "here", "why", "what", "rt", "join",
                "claim", "make", "check", "don't", "dont", "big", "one", "how", "who", "now", "and", "but", "for"}
_CASHTAG = re.compile(r"\$[A-Za-z][A-Za-z0-9]{1,9}\b")
_QUERY_TERMS = {"solana", "bitcoin", "ethereum", "fed", "fomc", "powell", "trump", "senate", "congress", "polymarket",
                "kalshi", "cpi", "memecoin", "$sol", "$btc", "$eth", "white house", "supreme court"}


def post_entities(text: str) -> set[str]:
    tags = {t.upper() for t in _CASHTAG.findall(text)}
    names = {w for w in re.findall(r"\b[A-Z][a-zA-Z]{2,}(?:\s[A-Z][a-zA-Z]{2,})?\b", text)
             if w.split()[0].lower() not in _ENTITY_STOP}
    return tags | names


def detect_bursts(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for (ent,) in conn.execute("SELECT DISTINCT entity FROM x_counts").fetchall():
        rows = conn.execute("SELECT start, count, venue, market_id, topic FROM x_counts WHERE entity=? AND start<=? "
                            "ORDER BY start", (ent, now.isoformat())).fetchall()
        if len(rows) < 12:
            continue
        series = [float(r[1]) for r in rows]
        last_start = _ts(rows[-1][0])
        if last_start and timedelta(0) <= now - last_start < timedelta(hours=1):
            frac = max(0.25, (now - last_start).total_seconds() / 3600)  # partial hour, rate-scaled
            series[-1] = series[-1] / frac
        z, rate, base = zscore(series)
        if z < 3:
            continue
        began = rows[-1][0]
        for i in range(len(series) - 2, max(0, len(series) - 7), -1):
            if zscore(series[:i + 1])[0] >= 2:
                began = rows[i][0]
            else:
                break
        found.append({"entity": ent, "source": "x_counts", "z": z, "rate": round(rate, 1), "baseline": base,
                      "began_at": began, "venue": rows[-1][2], "market_id": rows[-1][3], "topic": rows[-1][4]})
    # the wire sample: entity mentions in the last 2h vs the prior 24h (hourly Poisson)
    recent: Counter[str] = Counter()
    prior: Counter[str] = Counter()
    authors: dict[str, set[str]] = defaultdict(set)
    for text, created, author in conn.execute(
            "SELECT text, created_at, author_handle FROM x_posts WHERE created_at>=? AND weight>=0.01 AND pump_risk<0.5",
            ((now - timedelta(hours=26)).isoformat(),)).fetchall():
        d = _ts(created)
        if d is None:
            continue
        for e in post_entities(text):
            if d >= now - timedelta(hours=2):
                recent[e] += 1
                authors[e].add(author.lower())
            else:
                prior[e] += 1
    # the wire is a query-driven sample, so query terms always look "hot": demand an established
    # baseline (seen before the window), several distinct authors and a higher bar than the counts API
    for e, c in recent.items():
        if c < 6 or len(authors[e]) < 4 or prior[e] < 3 or e.lower() in _QUERY_TERMS:
            continue
        mu = prior[e] / 24.0 * 2
        z = (c - mu) / math.sqrt(mu + 1.0)
        if z >= 4:
            found.append({"entity": e, "source": "wire", "z": round(z, 2), "rate": c / 2, "baseline": round(mu / 2, 2),
                          "began_at": (now - timedelta(hours=2)).isoformat(), "venue": None, "market_id": None,
                          "topic": None})
    return found


def price_burst(conn: sqlite3.Connection, b: dict[str, Any], now: datetime, prices: Prices) -> list[dict[str, Any]]:
    """Attach markets + stance direction; 'not_priced_in' when price barely moved since the burst began."""
    began = _ts(b["began_at"]) or now
    targets: list[tuple[str, str, str]] = []
    if b.get("venue") and b.get("market_id"):
        targets.append((b["venue"], b["market_id"], b["entity"]))
    ent = b["entity"].lstrip("$").upper()
    if ent in MAJORS:
        targets.append(("token", ent, f"${ent}"))
    needle = b["entity"].lstrip("$")
    if len(needle) >= 3:
        for venue, mid, title in conn.execute(
                "SELECT l.venue, l.market_id, MAX(l.title) FROM x_market_links l JOIN x_posts p USING (post_id) "
                "WHERE p.created_at>=? AND p.text LIKE ? AND l.title LIKE ? AND l.venue!='token' GROUP BY 1, 2 "
                "ORDER BY COUNT(*) DESC LIMIT 3",
                ((now - timedelta(hours=6)).isoformat(), f"%{needle}%", f"%{needle}%")).fetchall():
            if (venue, mid) not in {(v, m) for v, m, _ in targets}:
                targets.append((venue, mid, title))
    out = []
    for venue, mid, title in targets:
        st = conn.execute("SELECT SUM(s.stance*s.confidence), SUM(s.confidence) FROM x_stance s JOIN x_posts p "
                          "USING (post_id) WHERE s.venue=? AND s.market_id=? AND s.relevant=1 AND p.created_at>=?",
                          (venue, mid, (began - timedelta(hours=1)).isoformat())).fetchone() \
            if _has(conn, "x_stance") else (None, None)
        direction = float(st[0]) / float(st[1]) if st and st[1] else 0.0
        tk = _token_key(venue, mid)
        if tk:
            p_then, p_now = prices.token(tk, began, tol_h=2), prices.token(tk, now, tol_h=2)
            move = (p_now - p_then) / p_then if p_then and p_now else None
            priced = None if move is None else abs(move) >= 0.01
        else:
            p_then, p_now = prices.market(venue, mid, began), prices.latest_market(venue, mid)
            move = (p_now - p_then) if p_then is not None and p_now is not None else None
            priced = None if move is None else abs(move) >= 0.02
        if priced is None:
            status = "no_price"
        elif priced:
            status = "priced_in"
        else:
            status = "not_priced_in" if abs(direction) >= 0.15 else "no_direction"
        conn.execute("INSERT INTO x_bursts (at, entity, source, z, rate, baseline, began_at, direction, venue, market_id, "
                     "title, price_then, price_now, price_move, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (now.isoformat(), b["entity"], b["source"], b["z"], b["rate"], b["baseline"], b["began_at"],
                      round(direction, 3), venue, mid, sanitize(title or "", 160), p_then, p_now,
                      None if move is None else round(move, 4), status))
        if status == "not_priced_in" and p_now is not None:
            nudge = 0.04 * (1 if direction > 0 else -1) * min(1.0, b["z"] / 6)
            why = {"z": b["z"], "direction": round(direction, 3), "entity": b["entity"]}
            if tk:
                record(conn, AGENTS["burst"], "x_token", f"major:{mid}", 0.5 + 2 * nudge, p_now, reasoning=why,
                       confidence=min(1.0, b["z"] / 6), now=now, token=True)
            else:
                record(conn, AGENTS["burst"], venue, mid, p_now + nudge, p_now, reasoning=why,
                       confidence=min(1.0, b["z"] / 6), now=now)
        out.append({"entity": b["entity"], "z": b["z"], "venue": venue, "market_id": mid, "title": title,
                    "direction": round(direction, 3), "move": move, "status": status})
    if not targets:
        conn.execute("INSERT INTO x_bursts (at, entity, source, z, rate, baseline, began_at, status) "
                     "VALUES (?,?,?,?,?,?,?,?)", (now.isoformat(), b["entity"], b["source"], b["z"], b["rate"],
                                                  b["baseline"], b["began_at"], "no_market"))
        out.append({"entity": b["entity"], "z": b["z"], "status": "no_market"})
    return out


# ======================================================================= 4. narratives

SECTORS: dict[str, re.Pattern[str]] = {k: re.compile(v, re.IGNORECASE) for k, v in {
    "ai_agents": r"(\bai agents?\b|\bagentic\b|\bvirtuals\b|\$ai16z\b|\$virtual\b|\$fet\b|\$tao\b|\bbittensor\b)",
    "memecoins": r"(\bmeme ?coins?\b|\bpump\.?fun\b|\$pepe\b|\$doge\b|\$wif\b|\$bonk\b|\$shib\b|\bdegen\b)",
    "l1": r"(\$sol\b|\bsolana\b|\$eth\b|\bethereum\b|\$sui\b|\$apt\b|\$avax\b|\$ton\b|\$hype\b|\bhyperliquid\b)",
    "l2": r"(\blayer ?2\b|\bl2s?\b|\brollups?\b|\$arb\b|\barbitrum\b|\$op\b|\boptimism\b|\bzksync\b|\$strk\b)",
    "defi": r"(\bdefi\b|\bdex\b|\blending\b|\$uni\b|\$aave\b|\byield\b|\btvl\b|\bperps?\b|\$jup\b|\$pendle\b)",
    "rwa": r"(\brwa\b|\breal[- ]world assets?\b|\btokeniz\w+|\$ondo\b|\bbuidl\b)",
    "depin": r"(\bdepin\b|\$hnt\b|\bhelium\b|\$render\b)",
    "btc_eco": r"(\bordinals\b|\brunes\b|\bbitcoin l2\b|\$stx\b|\bbtcfi\b|\bbabylon\b)",
    "stablecoins": r"(\bstablecoins?\b|\busdc\b|\busdt\b|\btether\b|\bgenius act\b)",
    "prediction_mkts": r"(\bpolymarket\b|\bkalshi\b|\bprediction markets?\b)",
    "etf_flows": r"(\betfs?\b|\binflows?\b|\boutflows?\b|\bibit\b|\bgrayscale\b)",
    "security": r"(\bhack(ed)?\b|\bexploit(ed)?\b|\bdrained\b|\brug(ged|pull)?\b|\bphishing\b)",
}.items()}
_EXTRA_STOP = {"https", "http", "amp", "just", "like", "get", "got", "now", "one", "new", "today", "going",
               "people", "via", "it's", "i'm", "don't", "can't", "here", "still", "make", "know", "has", "have",
               "not", "but", "all", "your", "you", "our", "they", "their", "its", "about", "into", "out", "been"}


def _terms(text: str) -> list[str]:
    t = re.sub(r"https?://\S+|@\w+", " ", text.lower())
    out = [w.strip(".'-") for w in re.findall(r"\$?[a-z][a-z0-9'.$-]{2,}", t)]
    return [w for w in out if len(w) >= 3 and w not in STOP and w not in _EXTRA_STOP and not w.isdigit()]


def cluster_posts(texts: list[str], *, threshold: float = 0.28) -> list[list[int]]:
    """TF-IDF + greedy leader clustering (deterministic, dependency-free)."""
    tfs = [Counter(_terms(t)) for t in texts]
    df = Counter(w for tf in tfs for w in tf)
    n = len(texts)
    vecs = []
    for tf in tfs:
        v = {w: (1 + math.log(c)) * math.log((n + 1) / (df[w] + 0.5)) for w, c in tf.items() if df[w] >= 2}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({w: x / norm for w, x in v.items()})
    clusters: list[tuple[dict[str, float], list[int]]] = []
    for i in sorted(range(n), key=lambda i: -len(vecs[i])):
        v = vecs[i]
        if not v:
            continue
        best, sim = None, threshold
        for ci, (cen, _) in enumerate(clusters):
            s = sum(x * cen.get(w, 0.0) for w, x in v.items())
            if s >= sim:
                best, sim = ci, s
        if best is None:
            clusters.append((dict(v), [i]))
            continue
        cen, mem = clusters[best]
        mem.append(i)
        k = len(mem)
        for w in set(cen) | set(v):
            cen[w] = cen.get(w, 0.0) * (k - 1) / k + v.get(w, 0.0) / k
    return [m for _, m in clusters]


def narrative_phase(first: datetime, now: datetime, now6: float, prev6: float, peak: float) -> str:
    if now - first <= timedelta(hours=6):
        return "birth"
    if now6 >= 3 and now6 >= 1.5 * max(prev6, 1):
        return "accelerating"
    if peak and now6 <= 0.5 * peak:
        return "decaying"
    return "peak"


def update_narratives(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    rows = conn.execute("SELECT post_id, text, created_at, LOWER(author_handle) FROM x_posts WHERE created_at>=? "
                        "AND pump_risk<0.5", ((now - timedelta(hours=48)).isoformat(),)).fetchall()
    if len(rows) < 6:
        return {"narratives": 0}
    stance = dict(conn.execute("SELECT post_id, AVG(stance*confidence) FROM x_stance GROUP BY post_id").fetchall()) \
        if _has(conn, "x_stance") else {}
    existing = conn.execute("SELECT id, terms FROM x_narratives WHERE last_seen>=?",
                            ((now - timedelta(days=4)).isoformat(),)).fetchall()
    kept = 0
    for g in cluster_posts([r[1] for r in rows]):
        posts, kept_sh = [], []
        for i in g:  # copy-paste duplicates count once: a ring is not a narrative
            sh = _shingles(rows[i][1])
            if any(sh and k and len(sh & k) / len(sh | k) >= 0.5 for k in kept_sh):
                continue
            kept_sh.append(sh)
            posts.append(rows[i])
        authors = {p[3] for p in posts}
        if len(posts) < 3 or len(authors) < 2:
            continue
        tf = Counter(w for p in posts for w in set(_terms(p[1])))
        terms = [w for w, c in tf.most_common(8) if c >= 2][:6]
        if len(terms) < 2:
            continue
        times = sorted(d for d in (_ts(p[2]) for p in posts) if d)
        first, last = times[0], times[-1]
        now6 = float(sum(1 for d in times if d >= now - timedelta(hours=6)))
        prev6 = float(sum(1 for d in times if now - timedelta(hours=12) <= d < now - timedelta(hours=6)))
        text = " ".join(p[1] for p in posts)
        hits = Counter({k: len(p.findall(text)) for k, p in SECTORS.items()})
        top = hits.most_common(1)
        sector = top[0][0] if top and top[0][1] else None
        tokens = [t for t, _ in Counter(t.upper() for t in _CASHTAG.findall(text)).most_common(5)]
        st = [stance[p[0]] for p in posts if stance.get(p[0]) is not None]
        nid = None
        for eid, eterms in existing:
            et = set(json.loads(eterms or "[]"))
            if et and len(et & set(terms)) / len(et | set(terms)) >= 0.34:
                nid = eid
                break
        peak = max(now6, prev6)
        if nid:
            prev = conn.execute("SELECT first_seen, peak_rate FROM x_narratives WHERE id=?", (nid,)).fetchone()
            first = min(first, _ts(prev[0]) or first)
            peak = max(float(prev[1] or 0), peak)
        vals = (" / ".join(terms[:3]), json.dumps(terms), sector, json.dumps(tokens), first.isoformat(),
                last.isoformat(), len(posts), len(authors), now6, prev6, peak,
                narrative_phase(first, now, now6, prev6, peak), round(sum(st) / len(st), 3) if st else None,
                sanitize(max(posts, key=lambda p: len(p[1]))[1], 200), now.isoformat())
        if nid:
            conn.execute("UPDATE x_narratives SET label=?, terms=?, sector=?, tokens=?, first_seen=?, last_seen=?, "
                         "posts=?, authors=?, rate_now=?, rate_prev=?, peak_rate=?, phase=?, stance=?, sample=?, "
                         "updated_at=? WHERE id=?", (*vals, nid))
        else:
            conn.execute("INSERT INTO x_narratives (label, terms, sector, tokens, first_seen, last_seen, posts, authors, "
                         "rate_now, rate_prev, peak_rate, phase, stance, sample, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
        kept += 1
    return {"narratives": kept}


def sector_rotation(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Share of crypto voice per sector, last 24h vs the 24h before (positive = rotating in)."""
    cur: Counter[str] = Counter()
    prev: Counter[str] = Counter()
    for text, created in conn.execute("SELECT text, created_at FROM x_posts WHERE topic='crypto' AND created_at>=? "
                                      "AND pump_risk<0.5", ((now - timedelta(hours=48)).isoformat(),)).fetchall():
        d = _ts(created)
        if d is None:
            continue
        bucket = cur if d >= now - timedelta(hours=24) else prev
        for k, p in SECTORS.items():
            if p.search(text):
                bucket[k] += 1
    tc, tp = sum(cur.values()) or 1, sum(prev.values()) or 1
    out = [{"sector": k, "mentions_24h": cur[k], "share_24h": round(cur[k] / tc, 3),
            "share_prev_24h": round(prev[k] / tp, 3), "rotation": round(cur[k] / tc - prev[k] / tp, 3)}
           for k in SECTORS]
    return sorted(out, key=lambda r: -r["rotation"])


# ======================================================================= 5. manipulation shield

_SOL_CA = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
_EVM_CA = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_PAID = re.compile(r"(#ad\b|#sponsored|\bsponsored\b|\bpaid partnership\b|\bpartnered with\b|"
                   r"\bclaim (your|the) (allocation|airdrop)|\bclaim now\b|\bpresale\b|\bwhitelist\b|"
                   r"\b\d{1,4}(\.\d)?x\b.{0,40}\bcall\b|\bcalled (it )?early\b)", re.IGNORECASE)
_CYR = re.compile(r"[\u0400-\u04FF]")
_LAT = re.compile(r"[A-Za-z]")
_MAJOR_TAGS = {"$BTC", "$ETH", "$SOL", "$USDC", "$USDT"}


def contracts_in(text: str) -> list[tuple[str, str]]:
    out = [("evm", m.group(0).lower()) for m in _EVM_CA.finditer(text)]
    for m in _SOL_CA.finditer(text):
        s = m.group(0)
        if any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
            out.append(("solana", s))
    return out


def homoglyph(text: str) -> bool:
    """Words mixing Cyrillic and Latin letters (filter evasion used by drainer / airdrop spam)."""
    return any(_CYR.search(w) and _LAT.search(w) for w in text.split())


def _shingles(text: str, k: int = 4) -> set[str]:
    w = re.findall(r"[a-z]{2,}", re.sub(r"https?://\S+|@\w+|\$\w+|\d+", " ", text.lower()))
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def shield(conn: sqlite3.Connection, now: datetime) -> list[dict[str, Any]]:
    """Coordinated-campaign detection over the last 24h of the wire."""
    posts = []
    for pid, text, created, author, fol, flags in conn.execute(
            "SELECT post_id, text, created_at, LOWER(author_handle), author_followers, flags FROM x_posts "
            "WHERE created_at>=?", ((now - timedelta(hours=24)).isoformat(),)).fetchall():
        d = _ts(created)
        if d is None:
            continue
        tags = frozenset(t.upper() for t in _CASHTAG.findall(text))
        posts.append({"id": pid, "text": text, "t": d, "a": author, "f": int(fol or 0),
                      "new": "new_account" in (flags or ""), "tags": tags, "sh": _shingles(text),
                      "ents": set(tags) | {f"{c}:{a}" for c, a in contracts_in(text)},
                      "paid": bool(_PAID.search(text)), "glyph": homoglyph(text)})
    reasons: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"score": 0.0, "reasons": set(), "authors": set(), "posts": set(), "first": None, "sample": ""})

    def flag(ent: str, why: str, w: float, ps: list[dict[str, Any]]) -> None:
        r = reasons[ent]
        if why not in r["reasons"]:
            r["score"] = min(1.0, r["score"] + w)
            r["reasons"].add(why)
        r["authors"] |= {p["a"] for p in ps}
        r["posts"] |= {p["id"] for p in ps}
        first = min(p["t"] for p in ps)
        r["first"] = first if r["first"] is None else min(r["first"], first)
        r["sample"] = r["sample"] or sanitize(ps[0]["text"], 160)

    for i, p in enumerate(posts):  # copy-paste rings: near-duplicate text from >=3 distinct authors
        if len(p["sh"]) < 3:
            continue
        ring = [p] + [q for q in posts[i + 1:] if q["a"] != p["a"] and q["sh"]
                      and len(p["sh"] & q["sh"]) / len(p["sh"] | q["sh"]) >= 0.5]
        if len({q["a"] for q in ring}) >= 3:
            ents = set().union(*(q["ents"] for q in ring)) - _MAJOR_TAGS
            for e in ents or {"text:" + " ".join(min(p["sh"]).split()[:3])}:
                flag(e, "copy_paste_ring", 0.45, ring)
    by_ent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in posts:
        for e in p["ents"] - _MAJOR_TAGS:
            by_ent[e].append(p)
    for e, ps in by_ent.items():
        ps.sort(key=lambda p: p["t"])
        for i, p in enumerate(ps):  # new-account / low-follower swarm within 2h
            win = [q for q in ps[i:] if q["t"] - p["t"] <= timedelta(hours=2)]
            if len({q["a"] for q in win if q["new"] or q["f"] < 500}) >= 3:
                flag(e, "new_account_swarm", 0.35, win)
                break
        promo = [p for p in ps if p["paid"]]
        if promo:
            flag(e, "paid_promo_language", 0.3, promo)
        if ":" in e:  # paid-KOL pattern: >=3 accounts with 10k+ followers posting the same CA within 30 min
            for i, p in enumerate(ps):
                win = [q for q in ps[i:] if q["t"] - p["t"] <= timedelta(minutes=30) and q["f"] >= 10_000]
                if len({q["a"] for q in win}) >= 3:
                    flag(e, "kol_synchronized_ca", 0.4, win)
                    break
        glyph = [p for p in ps if p["glyph"]]
        if glyph:
            flag(e, "homoglyph_obfuscation", 0.3, glyph)
    bundles: dict[frozenset[str], list[dict[str, Any]]] = defaultdict(list)
    for p in posts:
        if len(p["tags"] - _MAJOR_TAGS) >= 2:
            bundles[p["tags"]].append(p)
    for tags, ps in bundles.items():  # synchronized cashtag bundles: same set from >=4 authors within 1h
        ps.sort(key=lambda p: p["t"])
        for i, p in enumerate(ps):
            win = [q for q in ps[i:] if q["t"] - p["t"] <= timedelta(hours=1)]
            if len({q["a"] for q in win}) >= 4:
                for t in tags - _MAJOR_TAGS:
                    flag(t, "synchronized_cashtags", 0.3, win)
                break
    out = []
    for ent, r in reasons.items():
        if r["score"] < 0.3:
            continue
        kind = "text" if ent.startswith("text:") else ("cashtag" if ent.startswith("$") else "contract")
        conn.execute("INSERT OR REPLACE INTO x_shill_warnings (entity, kind, score, reasons, authors, posts, first_at, "
                     "updated_at, sample) VALUES (?,?,?,?,?,?,?,?,?)",
                     (ent, kind, round(r["score"], 3), json.dumps(sorted(r["reasons"])), len(r["authors"]),
                      len(r["posts"]), r["first"].isoformat() if r["first"] else None, now.isoformat(), r["sample"]))
        out.append({"entity": ent, "score": round(r["score"], 3), "reasons": sorted(r["reasons"]),
                    "authors": sorted(r["authors"])})
    conn.commit()  # short txn: the caller does network work before its own commit
    return out


def _shield_authors(conn: sqlite3.Connection) -> dict[str, int]:
    """author -> number of shilled entities they pushed (feeds demotion)."""
    if not _has(conn, "x_shill_warnings"):
        return {}
    out: Counter[str] = Counter()
    for (e,) in conn.execute("SELECT entity FROM x_shill_warnings WHERE score>=0.5").fetchall():
        needle = e.split(":", 1)[1] if ":" in e else e
        if e.startswith("text:"):
            continue
        for (a,) in conn.execute("SELECT DISTINCT LOWER(author_handle) FROM x_posts WHERE text LIKE ?",
                                 (f"%{needle}%",)).fetchall():
            out[a] += 1
    return dict(out)


def _bump_warning(conn: sqlite3.Connection, ent: str, why: str, w: float, now: datetime) -> None:
    row = conn.execute("SELECT score, reasons FROM x_shill_warnings WHERE entity=?", (ent,)).fetchone()
    reasons = set(json.loads(row[1] or "[]")) if row else set()
    score = float(row[0] or 0) if row else 0.0
    if why not in reasons:
        reasons.add(why)
        score = min(1.0, score + w)
    conn.execute("INSERT INTO x_shill_warnings (entity, kind, score, reasons, authors, posts, first_at, updated_at) "
                 "VALUES (?,?,?,?,0,0,?,?) ON CONFLICT(entity) DO UPDATE SET score=excluded.score, "
                 "reasons=excluded.reasons, updated_at=excluded.updated_at",
                 (ent, "contract", round(score, 3), json.dumps(sorted(reasons)), now.isoformat(), now.isoformat()))


# ======================================================================= 6. KOL -> wallet cross-check

def collect_contracts(conn: sqlite3.Connection, now: datetime) -> int:
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for text, created, author, fol in conn.execute(
            "SELECT text, created_at, LOWER(author_handle), author_followers FROM x_posts WHERE created_at>=?",
            ((now - timedelta(hours=48)).isoformat(),)).fetchall():
        tags = [t[1:].upper() for t in _CASHTAG.findall(text)]
        for chain, addr in contracts_in(text):
            a = agg.setdefault((chain, addr), {"first": created, "authors": set(), "kol": set(), "n": 0,
                                               "sym": tags[0] if tags else None})
            a["n"] += 1
            a["authors"].add(author)
            if int(fol or 0) >= 10_000:
                a["kol"].add(author)
            a["first"] = min(a["first"], created)
    for (chain, addr), a in agg.items():
        sw = conn.execute("SELECT score FROM x_shill_warnings WHERE entity=?", (f"{chain}:{addr}",)).fetchone()
        conn.execute("INSERT INTO x_contracts (chain, address, symbol, first_at, mentions, authors, kol_authors, "
                     "shill_score) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(chain, address) DO UPDATE SET "
                     "mentions=excluded.mentions, authors=excluded.authors, kol_authors=excluded.kol_authors, "
                     "shill_score=excluded.shill_score, symbol=COALESCE(x_contracts.symbol, excluded.symbol)",
                     (chain, addr, a["sym"], a["first"], a["n"], len(a["authors"]), len(a["kol"]),
                      float(sw[0]) if sw else 0.0))
    conn.commit()  # short txn: callers go back to the network afterwards
    return len(agg)


def dump_check(trades: list[dict[str, Any]], first_mention: datetime) -> dict[str, Any]:
    """GeckoTerminal pool trades: did wallets that bought before the first X mention sell into the hype?"""
    pre: dict[str, float] = defaultdict(float)
    post_sell: dict[str, float] = defaultdict(float)
    seen_before = 0
    for t in trades:
        a = t.get("attributes") if isinstance(t.get("attributes"), dict) else t
        who, kind, when = a.get("tx_from_address"), a.get("kind"), _ts(a.get("block_timestamp"))
        usd = _f(a.get("volume_in_usd")) or 0.0
        if not who or when is None:
            continue
        if when < first_mention:
            seen_before += 1
            if kind == "buy":
                pre[who] += usd
        elif kind == "sell":
            post_sell[who] += usd
    early = {w: v for w, v in pre.items() if v > 0}
    dumped = [w for w in early if post_sell.get(w, 0) >= 0.5 * early[w]]
    bought = sum(early.values())
    sold = sum(min(post_sell.get(w, 0), early[w] * 3) for w in early)
    share = sold / bought if bought else 0.0
    if not early:
        verdict = "window_after_mention" if not seen_before else "no_early_buyers_seen"
    elif len(dumped) >= max(2, len(early) // 3) or share >= 0.5:
        verdict = "early_buyers_dumped_into_hype"
    else:
        verdict = "early_buyers_holding"
    return {"early_buyers": len(early), "dumped_wallets": len(dumped), "early_buy_usd": round(bought, 2),
            "early_sold_usd": round(sold, 2), "sold_share": round(share, 3), "trades_seen": len(trades),
            "verdict": verdict}


def check_contracts(conn: sqlite3.Connection, now: datetime, *, fetch: bool = True, max_checks: int = 4,
                    client: Any = None, pause: float = 2.1) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT chain, address, first_at FROM x_contracts WHERE checked_at IS NULL OR checked_at<? "
                        "ORDER BY authors DESC, mentions DESC, first_at DESC LIMIT ?",
                        ((now - timedelta(hours=6)).isoformat(), max_checks)).fetchall()
    if not fetch or not rows:
        return []
    import httpx
    own = client is None
    c = client or httpx.Client(timeout=8, headers={"accept": "application/json"})
    out = []
    try:
        for chain, addr, first_at in rows:
            res: dict[str, Any] = {"verdict": "no_pool_found"}
            pool = price = liq = None
            for net in (["solana"] if chain == "solana" else ["base", "eth", "bsc"]):
                try:
                    r = c.get(f"{GT}/networks/{net}/tokens/{addr}/pools", params={"page": 1})
                    time.sleep(pause)  # GeckoTerminal public limit (~30/min)
                    data = (r.json().get("data") or []) if r.status_code == 200 else []
                    if not data:
                        continue
                    at = data[0].get("attributes") or {}
                    pool, price, liq = at.get("address"), at.get("base_token_price_usd"), at.get("reserve_in_usd")
                    tr = c.get(f"{GT}/networks/{net}/pools/{pool}/trades")
                    time.sleep(pause)
                    res = dump_check((tr.json().get("data") or []) if tr.status_code == 200 else [],
                                     _ts(first_at) or now)
                    res["network"] = net
                    break
                except Exception as exc:  # noqa: BLE001 - one bad CA never stops the run
                    res = {"verdict": f"unavailable: {type(exc).__name__}"}
            conn.execute("UPDATE x_contracts SET pool=?, price=?, liquidity=?, dump_json=?, verdict=?, checked_at=? "
                         "WHERE chain=? AND address=?",
                         (pool, _f(price), _f(liq), json.dumps(res), res.get("verdict"), now.isoformat(), chain, addr))
            if res.get("verdict") == "early_buyers_dumped_into_hype":
                _bump_warning(conn, f"{chain}:{addr}", "early_buyers_dumped", 0.5, now)
            conn.commit()  # never hold the noema.db write lock across the next contract's HTTP calls + sleeps
            out.append({"chain": chain, "address": addr, **res})
    finally:
        if own:
            c.close()
    return out


def push_to_alpha(conn: sqlite3.Connection, now: datetime) -> int:
    """Hand clean X contract mentions (>=2 authors) to the alpha screens. Shilled / dumped ones are withheld,
    and an X-sourced row we pushed earlier is withdrawn if the contract is later flagged."""
    if not _has(conn, "culture_signals"):
        return 0
    for chain, addr in conn.execute("SELECT chain, address FROM x_contracts WHERE pushed=1 AND (shill_score>=0.5 OR "
                                    "verdict='early_buyers_dumped_into_hype' OR (chain||':'||address) IN "
                                    "(SELECT entity FROM x_shill_warnings WHERE score>=0.5))").fetchall():
        ch = "solana" if chain == "solana" else "base"
        conn.execute("DELETE FROM culture_signals WHERE kind='token' AND entity=? AND sources='x'", (f"{ch}:{addr}",))
        conn.execute("UPDATE x_contracts SET pushed=-1 WHERE chain=? AND address=?", (chain, addr))
    n = 0
    for chain, addr, sym, first_at, mentions, authors, shill, verdict in conn.execute(
            "SELECT chain, address, symbol, first_at, mentions, authors, shill_score, verdict FROM x_contracts "
            "WHERE pushed=0 AND authors>=2 AND verdict IS NOT NULL").fetchall():
        if (shill or 0) >= 0.3 or verdict in ("early_buyers_dumped_into_hype", "no_pool_found"):
            continue
        ch = "solana" if chain == "solana" else "base"
        try:
            cur = conn.execute("INSERT OR IGNORE INTO culture_signals (kind, entity, symbol, chain, title, domain, buzz, "
                               "buzz_velocity, novelty, score, sources, first_seen, updated_at) "
                               "VALUES ('token',?,?,?,?,'crypto',?,?,1.0,?,'x',?,?)",
                               (f"{ch}:{addr}", sym, ch, f"X mention {sym or addr[:6]}", float(mentions),
                                float(authors), 0.1 * float(authors), first_at, now.isoformat()))
        except sqlite3.OperationalError:
            return n
        conn.execute("UPDATE x_contracts SET pushed=1 WHERE chain=? AND address=?", (chain, addr))
        n += cur.rowcount
    return n


# ======================================================================= 7. counter-signal

def crowd_extremes(conn: sqlite3.Connection, now: datetime, prices: Prices, *, min_n: int = 6) -> list[dict[str, Any]]:
    """One-sided euphoria / panic (>=6 posts, >=4 authors, |mean stance|>=0.6, >=85% one side) -> fade it."""
    by: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    titles: dict[tuple[str, str], str] = {}
    for _pid, venue, mid, st, conf, _src, author, _c, _pr, _f2, title in _stance_rows(
            conn, now - timedelta(hours=6), min_conf=0.3):
        by[(venue, mid)].append((float(st), float(conf), author.lower()))
        titles[(venue, mid)] = title or mid
    out = []
    for (venue, mid), xs in by.items():
        if len(xs) < min_n or len({a for _, _, a in xs}) < 4:
            continue
        mean = sum(s * c for s, c, _ in xs) / max(1e-9, sum(c for _, c, _ in xs))
        pos = sum(1 for s, _, _ in xs if s > 0.2)
        neg = sum(1 for s, _, _ in xs if s < -0.2)
        one = max(pos, neg) / max(1, pos + neg)
        if abs(mean) < 0.6 or one < 0.85:
            continue
        sig = "euphoria" if mean > 0 else "panic"
        conn.execute("INSERT INTO x_crowd (at, venue, target, title, n, mean_stance, one_side, signal) "
                     "VALUES (?,?,?,?,?,?,?,?)", (now.isoformat(), venue, mid, sanitize(titles[(venue, mid)], 160),
                                                  len(xs), round(mean, 3), round(one, 3), sig))
        tk = _token_key(venue, mid)
        price = prices.token(tk, now, tol_h=2) if tk else (None if venue == "token" else
                                                            prices.latest_market(venue, mid))
        why = {"crowd": sig, "n": len(xs), "mean_stance": round(mean, 3)}
        fade = -0.04 if mean > 0 else 0.04
        if price is not None and tk:
            record(conn, AGENTS["contra"], "x_token", f"major:{mid}", 0.5 + 2 * fade, price, reasoning=why,
                   confidence=one * abs(mean), now=now, token=True)
        elif price is not None:
            record(conn, AGENTS["contra"], venue, mid, price + fade, price, reasoning=why,
                   confidence=one * abs(mean), now=now)
        out.append({"venue": venue, "target": mid, "signal": sig, "n": len(xs), "mean": round(mean, 3)})
    return out


# ======================================================================= ledger hypotheses

def stance_forecasts(conn: sqlite3.Connection, now: datetime, prices: Prices) -> int:
    """x_social: market price nudged (<= MAX_NUDGE) by weighted stance toward YES (replaces lexicon)."""
    agg: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for venue, mid, st, conf, w, rel, src in conn.execute(
            "SELECT s.venue, s.market_id, s.stance, s.confidence, p.weight, l.relevance, s.source FROM x_stance s "
            "JOIN x_posts p USING (post_id) JOIN x_market_links l ON l.post_id=s.post_id AND l.venue=s.venue AND "
            "l.market_id=s.market_id WHERE s.relevant=1 AND p.created_at>=? AND s.venue!='token'",
            ((now - timedelta(hours=24)).isoformat(),)).fetchall():
        ww = float(w or 0) * float(rel or 0) * float(conf)
        a = agg[(venue, mid)]
        a[0] += ww * float(st)
        a[1] += ww
        a[2] += 1
        a[3] += 1 if src == "model" else 0
    n = 0
    for (venue, mid), (ws, w, cnt, model_n) in agg.items():
        if cnt < 2 or w <= 0:
            continue
        price = prices.latest_market(venue, mid)
        if price is None:
            continue
        lean = ws / w
        n += record(conn, AGENTS["stance"], venue, mid, price + MAX_NUDGE * lean * min(1.0, w * 4), price,
                    reasoning={"source": "x_social", "x_stance": round(lean, 3), "x_weight": round(w, 4),
                               "x_posts": int(cnt), "model_stances": int(model_n)},
                    confidence=min(1.0, w * 4), now=now)
    return n


def shield_forecasts(conn: sqlite3.Connection, now: datetime, prices: Prices) -> int:
    """x_shield: 'avoid, being shilled' as a measurable P(up) below 0.5 on the shilled token."""
    n = 0
    for ent, score in conn.execute("SELECT entity, score FROM x_shill_warnings WHERE score>=0.5 AND updated_at>=?",
                                   ((now - timedelta(hours=6)).isoformat(),)).fetchall():
        tag = ent.lstrip("$")
        if tag in MAJORS:
            px = prices.token(tag, now, tol_h=2)
            if px:
                n += record(conn, AGENTS["shield"], "x_token", f"major:{tag}", 0.4, px,
                            reasoning={"shill_score": score}, confidence=score, now=now, token=True)
        elif ":" in ent and not ent.startswith("text:"):
            row = conn.execute("SELECT price FROM x_contracts WHERE chain||':'||address=?", (ent,)).fetchone()
            if row and row[0]:
                n += record(conn, AGENTS["shield"], "x_token", ent, 0.3, float(row[0]),
                            reasoning={"shill_score": score, "verdict": "avoid, being shilled"},
                            confidence=score, now=now, token=True)
    return n


def resolve_x_tokens(conn: sqlite3.Connection, now: datetime, prices: Prices, db_path: str | None) -> int:
    """Resolve x_token forecasts after the accuracy horizon (majors: candles; contracts: fresh GT price)."""
    from .accuracy_ledger import resolve_token_marks
    if not _has(conn, "accuracy_forecasts"):
        return 0
    marks: dict[str, float] = {}
    for (mid,) in conn.execute("SELECT DISTINCT market_id FROM accuracy_forecasts WHERE venue='x_token' AND id NOT IN "
                               "(SELECT forecast_id FROM accuracy_resolutions)").fetchall():
        if mid.startswith("major:"):
            px = prices.token(mid.split(":", 1)[1], now, tol_h=1)
        else:
            row = conn.execute("SELECT price FROM x_contracts WHERE chain||':'||address=? AND checked_at>=?",
                               (mid, (now - timedelta(hours=2)).isoformat())).fetchone()
            px = float(row[0]) if row and row[0] else None
        if px:
            marks[mid] = px
    if not marks:
        return 0
    # only our own namespaced ids are in marks, so other agents' token forecasts are untouched
    return resolve_token_marks(conn, marks, now=now, db_path=db_path)


# ======================================================================= planner (adaptive queries)

CASHTAG_NAMES = {"BTC": '"bitcoin"', "ETH": '"ethereum"', "SOL": '"solana"', "SPY": '"S&P 500"',
                 "SPX": '"S&P 500"', "QQQ": '"Nasdaq 100"', "XRP": '"XRP"', "DOGE": '"dogecoin"'}
_Q_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b")


def precise_query(q: str) -> str:
    """X search strips '$' from cashtags, so '$SOL' degrades to the bare word 'SOL' (matches 'sol', 'SOL' the
    sun, etc.). Replace each cashtag with a quoted name (known majors) or a quoted ticker, then drop OR
    alternatives that became duplicates. Precision over recall."""
    def sub(m: re.Match[str]) -> str:
        t = m.group(1).upper()
        return CASHTAG_NAMES.get(t, f'"{t}"')
    out = _Q_CASHTAG.sub(sub, q)

    def dedupe(m: re.Match[str]) -> str:
        parts, seen = [], set()
        for a in (x.strip() for x in m.group(1).split(" OR ")):
            k = a.strip('"').lower()
            if k not in seen:
                seen.add(k)
                parts.append(a)
        return "(" + " OR ".join(parts) + ")"
    return re.sub(r"\(([^()]*)\)", dedupe, out)


def plan_queries(conn: sqlite3.Connection, cfg: dict[str, Any], now: datetime, *, calls_per_run: int = 12,
                 per_topic: int = 3) -> dict[str, Any]:
    """Same ``tev.x_sources.v1`` format. Base queries are rotated to ``per_topic`` per topic each 30-min slot,
    promoted smart-money authors get one ``from:`` query, and two counts queries (hottest markets, else
    majors) feed burst detection. 9 + 1 + 2 = 12 calls per run, 576/day, + trends ~24/day = ~600/day."""
    plan = json.loads(json.dumps(cfg))
    slot = int(now.timestamp() // 1800)
    scored = _has(conn, "x_author_scores")
    for name, t in (plan.get("topics") or {}).items():
        qs = list(t.get("queries") or [])
        if len(qs) > per_topic:
            k = slot % len(qs)
            t["queries"] = (qs[k:] + qs[:k])[:per_topic]
        t.setdefault("accounts", {})
        t["accounts"]["smart_money"] = [a for (a,) in conn.execute(
            "SELECT author FROM x_author_scores WHERE status='promoted' AND topics LIKE ? ORDER BY shrunk DESC "
            "LIMIT 12", (f"%{name}%",)).fetchall()] if scored else []
    top = [a for (a,) in conn.execute("SELECT author FROM x_author_scores WHERE status='promoted' ORDER BY shrunk DESC "
                                      "LIMIT 25").fetchall()] if scored else []
    demoted = [a for (a,) in conn.execute("SELECT author FROM x_author_scores WHERE status='demoted' ORDER BY "
                                          "shill_rate DESC LIMIT 15").fetchall()] if scored else []
    sm_q: list[str] = []
    q = ""
    for a in top:
        nxt = (q + " OR " if q else "") + f"from:{a}"
        if len(nxt) > 470:
            break
        q = nxt
    if q:
        sm_q.append(f"({q}) -is:retweet")
    plan["smart_money"] = {"topic": "crypto", "queries": sm_q, "promoted": top, "demoted_excluded": demoted}
    counts: list[dict[str, Any]] = []
    if _has(conn, "x_market_links"):
        from .news_crawler import market_query
        for venue, mid, title, _c in conn.execute(
                "SELECT venue, market_id, MAX(title), COUNT(*) FROM x_market_links WHERE linked_at>=? AND "
                "venue!='token' GROUP BY 1, 2 ORDER BY COUNT(*) DESC LIMIT 2",
                ((now - timedelta(hours=24)).isoformat(),)).fetchall():
            mq = market_query(title or "")
            if mq and len(mq.split()) >= 2:
                counts.append({"entity": f"{venue}:{mid}", "query": f"{mq} -is:retweet", "venue": venue,
                               "market_id": mid, "topic": "politics"})
    for ent, cq in (("BTC", "(bitcoin OR $BTC) lang:en -is:retweet"),
                    ("SOL", "(solana OR $SOL) lang:en -is:retweet")):
        if len(counts) < 2:
            counts.append({"entity": ent, "query": cq, "topic": "crypto"})
    plan["counts"] = [{**x, "query": precise_query(x["query"])} for x in counts[:2]]
    for t in (plan.get("topics") or {}).values():
        t["queries"] = [precise_query(x) for x in t.get("queries") or []]
    n_search = sum(len(t.get("queries") or []) for t in (plan.get("topics") or {}).values()) + len(sm_q)
    plan["budget"] = {**(plan.get("budget") or {}), "calls_per_run": calls_per_run, "daily_target": 600,
                      "planned": {"search": n_search, "counts": len(plan["counts"]),
                                  "trends_every_n_runs": (plan.get("trends") or {}).get("every_n_runs", 4)}}
    plan["planned_at"] = now.isoformat()
    plan["notes"] = [*list(plan.get("notes") or []), (
        "Adaptive plan from x_edge.plan_queries: run topic + smart_money queries with search_posts_all; run each "
        "counts entry with get_posts_counts_recent (granularity=hour) and drop it as {schema: tev.x_counts.v1, "
        "topic, entity, query, venue, market_id, fetched_at, raw}.")]
    return plan


def write_plan(db_path: str, path: str | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    from .x_signal import load_config
    conn = sqlite_connect(db_path)
    try:
        ensure_edge_schema(conn)
        plan = plan_queries(conn, cfg or load_config(), datetime.now(UTC))
    finally:
        conn.close()
    p = Path(path or os.getenv("NOEMA_X_PLAN_PATH", DEFAULT_PLAN))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, indent=1))
    tmp.replace(p)
    return plan


# ======================================================================= orchestration

def run_edge(db_path: str, *, now: datetime | None = None, fetch: bool | None = None, stance_client: Any = None,
             write_plan_file: bool | None = None) -> dict[str, Any]:
    from .x_stance import classify_pending, ensure_stance_schema
    now = now or datetime.now(UTC)
    fetch = os.getenv("NOEMA_X_EDGE_FETCH", "1") == "1" if fetch is None else fetch
    stats: dict[str, Any] = {}
    conn = sqlite_connect(db_path)
    try:
        if not _has(conn, "x_posts"):
            return {"status": "no x_posts"}
        ensure_edge_schema(conn)
        ensure_stance_schema(conn)
        stats["stance"] = classify_pending(conn, db_path=db_path, client=stance_client, now=now)
        prices = Prices(conn, fetch=fetch)
        stats["shill_warnings"] = len(shield(conn, now))
        stats["contracts"] = collect_contracts(conn, now)
        stats["contracts_checked"] = len(check_contracts(conn, now, fetch=fetch))
        stats["alpha_pushed"] = push_to_alpha(conn, now)
        stats["calls_new"] = collect_calls(conn, now, prices)
        stats["calls_resolved"] = resolve_calls(conn, now, prices)
        mark_early(conn)
        stats["authors_scored"] = score_authors(conn, now)
        stats.update(update_narratives(conn, now))
        alerts: list[dict[str, Any]] = []
        for b in detect_bursts(conn, now):
            alerts += price_burst(conn, b, now, prices)
        stats["bursts"] = len(alerts)
        stats["not_priced_in"] = sum(1 for a in alerts if a["status"] == "not_priced_in")
        stats["crowd_extremes"] = len(crowd_extremes(conn, now, prices))
        stats["ledger_forecasts"] = {"x_social": stance_forecasts(conn, now, prices),
                                     "x_smart_money": smart_money_forecasts(conn, now, prices),
                                     "x_shield": shield_forecasts(conn, now, prices)}
        stats["x_token_resolved"] = resolve_x_tokens(conn, now, prices, db_path)
        stats["price_calls"] = prices.calls
        conn.execute("DELETE FROM x_bursts WHERE at<?", ((now - timedelta(days=14)).isoformat(),))
        conn.execute("DELETE FROM x_crowd WHERE at<?", ((now - timedelta(days=14)).isoformat(),))
        conn.execute("DELETE FROM x_counts WHERE start<?", ((now - timedelta(days=10)).isoformat(),))
        conn.execute("INSERT INTO x_edge_runs (at, detail) VALUES (?,?)", (now.isoformat(), json.dumps(stats)))
        conn.commit()
    finally:
        conn.close()
    if write_plan_file if write_plan_file is not None else os.getenv("NOEMA_X_PLAN_WRITE", "1") == "1":
        try:
            write_plan(db_path)
            stats["plan"] = "written"
        except Exception as exc:  # noqa: BLE001 - planning never breaks ingestion
            stats["plan"] = f"failed: {type(exc).__name__}"
    return stats


# ======================================================================= read model (/api/x-signal)

def x_signal_board(db_path: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    base: dict[str, Any] = {"status": "no data", "role": "evidence only; never a sole trade trigger",
                            "leaderboard": [], "narratives": [], "alerts": [], "shill_warnings": [], "crowd": [],
                            "contracts": [], "rotation": [], "agents": [], "stance": {}, "last_run": None}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return base
    try:
        if not _has(conn, "x_edge_runs"):
            return {**base, "note": "X edge engine has not run yet"}
        q = conn.execute
        h48 = (now - timedelta(hours=48)).isoformat()
        lb = q("SELECT author, n, hits, brier, skill, shrunk, early_share, posts, shill_rate, status FROM x_author_scores "
               "WHERE status!='demoted' AND (n>0 OR status='promoted') ORDER BY (status='promoted') DESC, shrunk DESC, "
               "n DESC LIMIT 15").fetchall()
        demoted = q("SELECT author, posts, shill_rate FROM x_author_scores WHERE status='demoted' ORDER BY shill_rate DESC, "
                    "posts DESC LIMIT 8").fetchall()
        narr = q("SELECT id, label, sector, tokens, phase, posts, authors, rate_now, rate_prev, stance, first_seen, "
                 "last_seen, sample FROM x_narratives WHERE last_seen>=? ORDER BY (phase IN ('birth','accelerating')) "
                 "DESC, rate_now DESC, posts DESC LIMIT 12", (h48,)).fetchall()
        al = q("SELECT at, entity, z, direction, venue, market_id, title, price_then, price_now, price_move, status "
               "FROM x_bursts WHERE at>=? ORDER BY (status='not_priced_in') DESC, at DESC, z DESC LIMIT 12",
               ((now - timedelta(hours=12)).isoformat(),)).fetchall()
        sw = q("SELECT entity, kind, score, reasons, authors, posts, first_at, sample FROM x_shill_warnings WHERE "
               "updated_at>=? ORDER BY score DESC, posts DESC LIMIT 12", (h48,)).fetchall()
        cr = q("SELECT at, venue, target, title, n, mean_stance, one_side, signal FROM x_crowd WHERE at>=? "
               "ORDER BY id DESC LIMIT 8", ((now - timedelta(hours=24)).isoformat(),)).fetchall()
        ca = q("SELECT chain, address, symbol, mentions, authors, kol_authors, shill_score, verdict, dump_json, price, "
               "liquidity, pushed FROM x_contracts ORDER BY first_at DESC LIMIT 12").fetchall()
        st = q("SELECT source, COUNT(*) FROM x_stance GROUP BY source").fetchall() if _has(conn, "x_stance") else []
        last = q("SELECT at, detail FROM x_edge_runs ORDER BY id DESC LIMIT 1").fetchone()
        posts = []
        for r in q("SELECT p.post_id, p.author_handle, p.author_class, p.author_followers, p.text, p.created_at, "
                   "p.topic, p.url, p.credibility, p.pump_risk, p.weight, p.flags, a.status, a.shrunk, a.n, a.hits, "
                   "p.likes, p.reposts, p.replies, p.quotes, p.velocity, p.ingested_at "
                   "FROM x_posts p LEFT JOIN x_author_scores a ON a.author=LOWER(p.author_handle) WHERE p.weight>=0.01 "
                   "AND p.created_at>=? ORDER BY (p.post_id IN (SELECT post_id FROM x_market_links)) DESC, "
                   "p.created_at DESC LIMIT 30", (h48,)).fetchall():
            pst = q("SELECT s.venue, s.market_id, l.title, s.stance, s.confidence, s.source FROM x_stance s "
                   "LEFT JOIN x_market_links l ON l.post_id=s.post_id AND l.venue=s.venue AND l.market_id=s.market_id "
                   "WHERE s.post_id=? AND s.relevant=1 ORDER BY s.confidence DESC LIMIT 3", (r[0],)).fetchall() \
                if _has(conn, "x_stance") else []
            posts.append({"id": r[0], "author": r[1], "author_class": r[2], "followers": r[3],
                          "text": sanitize(r[4], 280), "created_at": r[5], "topic": r[6], "url": r[7],
                          "credibility": r[8], "pump_risk": r[9], "weight": r[10],
                          "flags": [f for f in (r[11] or "").split(",") if f],
                          "smart_money": {"status": r[12] or "unscored", "shrunk": r[13], "n": r[14] or 0,
                                          "hits": r[15] or 0},
                          "engagement": {"likes": r[16], "reposts": r[17], "replies": r[18], "quotes": r[19]},
                          "velocity": r[20], "ingested_at": r[21],
                          "stances": [{"venue": v, "market_id": m, "title": sanitize(t or m, 90), "stance": sv,
                                       "confidence": cf, "source": src} for v, m, t, sv, cf, src in pst]})
        agents = []
        if _has(conn, "accuracy_forecasts") and _has(conn, "accuracy_resolutions"):
            for agent in AGENTS.values():
                r = q("SELECT COUNT(*), COUNT(r.forecast_id), AVG(r.brier), AVG(r.hit) FROM accuracy_forecasts f "
                      "LEFT JOIN accuracy_resolutions r ON r.forecast_id=f.id WHERE f.agent_id=?", (agent,)).fetchone()
                agents.append({"agent": agent, "forecasts": r[0], "resolved": r[1] or 0,
                               "brier": None if r[2] is None else round(r[2], 4),
                               "hit_rate": None if r[3] is None else round(r[3], 3)})
        rotation = sector_rotation(conn, now)
    except sqlite3.OperationalError as exc:
        return {**base, "note": f"x edge tables incomplete: {type(exc).__name__}"}
    finally:
        conn.close()
    return {
        **base, "status": "active", "as_of": last[0] if last else None,
        "last_run": json.loads(last[1]) if last else None,
        "leaderboard": [{"author": a, "n": n, "hits": h, "brier": b, "skill": s, "shrunk": sh, "early_share": e,
                         "posts": p, "shill_rate": sr, "status": stt} for a, n, h, b, s, sh, e, p, sr, stt in lb],
        "demoted": [{"author": a, "posts": p, "shill_rate": sr} for a, p, sr in demoted],
        "narratives": [{"id": i, "label": lbl, "sector": sec, "tokens": json.loads(tk or "[]"), "phase": ph,
                        "posts": n, "authors": au, "rate_now": rn, "rate_prev": rp, "stance": s,
                        "first_seen": fs, "last_seen": ls, "sample": smp}
                       for i, lbl, sec, tk, ph, n, au, rn, rp, s, fs, ls, smp in narr],
        "alerts": [{"at": a, "entity": e, "z": z, "direction": d, "venue": v, "market_id": m, "title": t,
                    "price_then": pt, "price_now": pn, "price_move": pm, "status": s,
                    "headline": "NOT PRICED IN" if s == "not_priced_in" else (s or "").replace("_", " ")}
                   for a, e, z, d, v, m, t, pt, pn, pm, s in al],
        "shill_warnings": [{"entity": e, "kind": k, "score": s, "reasons": json.loads(r or "[]"), "authors": au,
                            "posts": p, "first_at": f, "sample": smp,
                            "verdict": "avoid, being shilled" if (s or 0) >= 0.5 else "caution"}
                           for e, k, s, r, au, p, f, smp in sw],
        "crowd": [{"at": a, "venue": v, "target": t, "title": ti, "n": n, "mean_stance": m, "one_side": o,
                   "signal": s, "contrarian": "fade"} for a, v, t, ti, n, m, o, s in cr],
        "contracts": [{"chain": c, "address": a, "symbol": s, "mentions": m, "authors": au, "kol_authors": k,
                       "shill_score": sh, "verdict": v, "dump": json.loads(d) if d else None, "price": p,
                       "liquidity": lq, "sent_to_alpha": pu == 1,
                       "withdrawn_from_alpha": pu == -1} for c, a, s, m, au, k, sh, v, d, p, lq, pu in ca],
        "rotation": rotation, "agents": agents, "stance": dict(st), "posts": posts,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="X edge engine (evidence only)")
    ap.add_argument("cmd", choices=["run", "plan", "board"])
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    if a.cmd == "run":
        print(json.dumps(run_edge(a.db, fetch=not a.no_fetch), default=str))
    elif a.cmd == "plan":
        print(json.dumps(write_plan(a.db), indent=1))
    else:
        print(json.dumps(x_signal_board(a.db), indent=1, default=str))


if __name__ == "__main__":
    main()


def maybe_run_edge(db_path: str, *, new_posts: int = 0, min_interval_s: int | None = None) -> dict[str, Any]:
    """Crawler hook: run right after new X posts arrive, otherwise at most every ``min_interval_s``
    (default 15 min) so 1h / 24h outcomes, bursts and token marks keep resolving."""
    interval = min_interval_s if min_interval_s is not None else int(os.getenv("NOEMA_X_EDGE_INTERVAL_S", "900"))
    if not new_posts:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute("SELECT MAX(at) FROM x_edge_runs").fetchone() if _has(conn, "x_edge_runs") else None
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        last = _ts(row[0]) if row and row[0] else None
        if last and (datetime.now(UTC) - last).total_seconds() < interval:
            return {"status": "skipped", "next_in_s": int(interval - (datetime.now(UTC) - last).total_seconds())}
    return run_edge(db_path)
