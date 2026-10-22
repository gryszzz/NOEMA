"""Culture/flow crawler: crypto, stock and prediction-market buzz, read-only.

Every source is a free public JSON endpoint. Each host's robots.txt is checked
first (a disallow means the source is skipped and reported as blocked), each host
is rate-limited, and every string is treated as untrusted (sanitized, bounded,
injection phrases redacted) before it is stored. Nothing here trades.

Observations are normalised into a *culture signal* per entity (token, ticker or
market): buzz velocity, sentiment and novelty. Market signals are recorded in the
accuracy ledger as agent ``culture`` so the accuracy engine measures whether
culture adds edge over the market price.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import threading
import time
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from .crawl_http import (
    HostGate,
    LiveFeed,
    SweepMeter,
    adaptive_sleep,
    fetch_concurrently,
    retry_after_seconds,
)
from .news_crawler import sanitize
from .sqlite_util import connect as sqlite_connect

USER_AGENT = "TEV-culture-crawler/1.0 (read-only research; +https://github.com/gryszzz/TEV-Bot-Trading-Terminal)"
DEFAULT_SPACING = 2.0
HOST_SPACING = {"api.stocktwits.com": 20.0, "apewisdom.io": 10.0, "api.rugcheck.xyz": 1.5,
                "api.gopluslabs.io": 2.0, "frontend-api-v3.pump.fun": 10.0}
CULTURE_AGENT = "culture"
CULTURE_NUDGE = 0.04  # max probability shift culture may apply to a market price


@dataclass(frozen=True)
class Source:
    name: str
    domain: str  # crypto | stocks | prediction
    url: str
    note: str = ""


SOURCES: tuple[Source, ...] = (
    Source("dexscreener_boosts_top", "crypto", "https://api.dexscreener.com/token-boosts/top/v1"),
    Source("dexscreener_boosts_latest", "crypto", "https://api.dexscreener.com/token-boosts/latest/v1"),
    Source("dexscreener_profiles", "crypto", "https://api.dexscreener.com/token-profiles/latest/v1"),
    Source("jupiter_trending_1h", "crypto", "https://lite-api.jup.ag/tokens/v2/toptrending/1h?limit=50"),
    Source("pumpfun_live", "crypto", "https://frontend-api-v3.pump.fun/coins/currently-live?limit=50"),
    Source("apewisdom_crypto", "crypto", "https://apewisdom.io/api/v1.0/filter/all-crypto/page/1",
           "Reddit crypto mention counts (r/CryptoCurrency, r/solana, ...) aggregated by ApeWisdom"),
    Source("apewisdom_wsb", "stocks", "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1",
           "r/wallstreetbets mention counts via ApeWisdom"),
    Source("apewisdom_stocks", "stocks", "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
           "Reddit stock mention counts (r/stocks, r/investing, ...) via ApeWisdom"),
    Source("stocktwits_trending", "stocks", "https://api.stocktwits.com/api/2/trending/symbols.json"),
    Source("kalshi_markets", "prediction",
           "https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000&mve_filter=exclude"),
    Source("polymarket_volume", "prediction",
           "https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume24hr&ascending=false&limit=100"),
    Source("polymarket_new", "prediction",
           "https://gamma-api.polymarket.com/markets?active=true&closed=false&order=createdAt&ascending=false&limit=50"),
    Source("polymarket_leaderboard", "prediction",
           "https://data-api.polymarket.com/v1/leaderboard?timePeriod=day&limit=25"),
)

# Probed 2026-10-21 and left out (reported on the room as blocked):
BLOCKED_SOURCES: tuple[dict[str, str], ...] = (
    {"name": "reddit_json", "url": "https://www.reddit.com/r/*/hot.json",
     "reason": "robots.txt disallows and returns 403; Reddit buzz comes via ApeWisdom instead"},
    {"name": "geckoterminal_trending", "url": "https://api.geckoterminal.com/api/v2/networks/trending_pools",
     "reason": "robots.txt disallows generic crawlers (OHLCV desks keep their own API limiter)"},
    {"name": "coingecko_trending", "url": "https://api.coingecko.com/api/v3/search/trending",
     "reason": "robots.txt disallows"},
    {"name": "birdeye_trending", "url": "https://public-api.birdeye.so/defi/token_trending",
     "reason": "401: needs an API key"},
    {"name": "yahoo_movers", "url": "https://query1.finance.yahoo.com/v1/finance/screener",
     "reason": "robots.txt disallows"},
    {"name": "nasdaq_movers", "url": "https://api.nasdaq.com/api/marketmovers", "reason": "times out from the box"},
    {"name": "sec_form4", "url": "https://www.sec.gov/cgi-bin/browse-edgar?type=4",
     "reason": "403 (SEC requires a contact-email User-Agent; not configured)"},
    {"name": "polymarket_comments", "url": "https://gamma-api.polymarket.com/comments",
     "reason": "returns empty without an event id; not crawled per market yet"},
)


class CultureClient:
    """HTTPS-only JSON GETs with robots.txt, per-host spacing, keep-alive pooling and status tracking.

    ``get_json`` is thread-safe: hosts run in parallel, each host serialized and spaced, and a
    429/503 puts that host on a Retry-After cooldown instead of hammering it.
    """

    def __init__(self, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 spacing: float = DEFAULT_SPACING) -> None:
        self.client = client or httpx.Client(
            timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=120))
        self.sleep, self.spacing = sleep, spacing
        self.gate = HostGate(spacing, HOST_SPACING, sleep=sleep)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self.status: dict[str, str] = {}
        self.meter: SweepMeter | None = None
        self.live = LiveFeed(None, "culture")
        self.source_of: dict[str, str] = {}
        self._etags: dict[str, tuple[str | None, str | None, Any]] = {}

    def _wait(self, host: str) -> None:
        self.gate.acquire(host).release()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return False
        with self._robots_lock:
            known = parts.netloc in self._robots
        if not known:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            sem = self.gate.acquire(parts.netloc)
            try:
                r = self.client.get(f"https://{parts.netloc}/robots.txt")
                if r.status_code >= 400:
                    rp = None
                else:
                    assert rp is not None
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None
            finally:
                sem.release()
            with self._robots_lock:
                self._robots[parts.netloc] = rp
        rp = self._robots[parts.netloc]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def get_json(self, url: str) -> Any:
        if not self.allowed(url):
            self.status[url] = "robots"
            return None
        host = urlsplit(url).netloc
        if self.gate.cooling(host):
            self.status[url] = "cooldown"
            return None
        headers: dict[str, str] = {}
        cached = self._etags.get(url)
        if cached and cached[0]:
            headers["If-None-Match"] = cached[0]
        if cached and cached[1]:
            headers["If-Modified-Since"] = cached[1]
        sem = self.gate.acquire(host)
        tok = self.live.start(host, self.source_of.get(url, urlsplit(url).path[:60]))
        try:
            r = self.client.get(url, headers=headers) if headers else self.client.get(url)
        except httpx.HTTPError as exc:
            self.status[url] = f"error:{type(exc).__name__}"
            if self.meter:
                self.meter.record(0)
            self.live.finish(tok, f"error:{type(exc).__name__}")
            return None
        finally:
            sem.release()
        self.live.finish(tok, r.status_code, len(r.content))
        if self.meter:
            self.meter.record(r.status_code)
        if r.status_code == 304 and cached:
            self.status[url] = "304"
            return cached[2]
        self.status[url] = str(r.status_code)
        if r.status_code in (429, 503):
            self.gate.cool(host, retry_after_seconds(r.headers.get("retry-after")))
        if r.status_code >= 400 or len(r.content) > 8_000_000:
            return None
        try:
            data = r.json()
        except ValueError:
            self.status[url] = "bad_json"
            return None
        etag, lm = r.headers.get("etag"), r.headers.get("last-modified")
        if (etag or lm) and len(r.content) < 2_000_000:
            self._etags[url] = (etag, lm, data)
        return data


SCHEMA = """
CREATE TABLE IF NOT EXISTS culture_observations (id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, source TEXT NOT NULL, domain TEXT NOT NULL, kind TEXT NOT NULL,
    entity TEXT NOT NULL, symbol TEXT, chain TEXT, title TEXT, buzz REAL NOT NULL,
    sentiment REAL, price REAL, price_change REAL, volume REAL, liquidity REAL, meta_json TEXT);
CREATE INDEX IF NOT EXISTS culture_obs_entity ON culture_observations(kind, entity, at);
CREATE TABLE IF NOT EXISTS culture_signals (kind TEXT NOT NULL, entity TEXT NOT NULL,
    symbol TEXT, chain TEXT, title TEXT, domain TEXT, buzz REAL, buzz_velocity REAL,
    sentiment REAL, novelty REAL, score REAL, sources TEXT, first_seen TEXT, updated_at TEXT,
    price REAL, price_change REAL, volume REAL, liquidity REAL, forecast_id INTEGER,
    PRIMARY KEY (kind, entity));
CREATE TABLE IF NOT EXISTS culture_source_state (name TEXT PRIMARY KEY, domain TEXT, url TEXT,
    status TEXT, items INTEGER, fetched_at TEXT);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@dataclass
class Obs:
    source: str
    domain: str
    kind: str  # token | ticker | market | trader
    entity: str
    buzz: float
    symbol: str | None = None
    chain: str | None = None
    title: str | None = None
    sentiment: float | None = None
    price: float | None = None
    price_change: float | None = None
    volume: float | None = None
    liquidity: float | None = None
    meta: dict[str, Any] | None = None


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _addr(value: Any) -> str | None:
    s = str(value or "")
    return s if 20 <= len(s) <= 64 and s.isalnum() else None


# -- parsers (pure; each takes decoded JSON and returns observations) -------------------------

def parse_dexscreener(name: str, data: Any) -> list[Obs]:
    out = []
    for i, row in enumerate(data if isinstance(data, list) else []):
        if not isinstance(row, dict):
            continue
        addr, chain = _addr(row.get("tokenAddress")), sanitize(row.get("chainId"), 20)
        if not addr or not chain:
            continue
        boost = _f(row.get("totalAmount")) or _f(row.get("amount")) or 0.0
        out.append(Obs(name, "crypto", "token", f"{chain}:{addr}", chain=chain,
                       title=sanitize(row.get("description"), 120) or None,
                       buzz=round(1.0 + math.log1p(boost) + max(0, 30 - i) / 30, 4),
                       meta={"boost": boost, "rank": i + 1}))
    return out


def parse_jupiter(name: str, data: Any) -> list[Obs]:
    out = []
    for i, row in enumerate(data if isinstance(data, list) else []):
        addr = _addr(row.get("id")) if isinstance(row, dict) else None
        if not addr:
            continue
        s1 = row.get("stats1h") or {}
        buys, sells = _f(s1.get("buyVolume")) or 0.0, _f(s1.get("sellVolume")) or 0.0
        senti = (buys - sells) / (buys + sells) if buys + sells > 0 else None
        out.append(Obs(name, "crypto", "token", f"solana:{addr}", chain="solana",
                       symbol=sanitize(row.get("symbol"), 16) or None, title=sanitize(row.get("name"), 80),
                       buzz=round(1.0 + max(0, 50 - i) / 25, 4), sentiment=senti,
                       price=_f(row.get("usdPrice")), price_change=_f(s1.get("priceChange")),
                       volume=buys + sells, liquidity=_f(row.get("liquidity")),
                       meta={"holders": row.get("holderCount"), "organic": row.get("organicScoreLabel"),
                             "mint_authority": bool(row.get("mintAuthority")),
                             "freeze_authority": bool(row.get("freezeAuthority")), "rank": i + 1}))
    return out


def parse_pumpfun(name: str, data: Any) -> list[Obs]:
    out = []
    for i, row in enumerate(data if isinstance(data, list) else []):
        addr = _addr(row.get("mint")) if isinstance(row, dict) else None
        if not addr or row.get("is_banned") or row.get("nsfw"):
            continue
        out.append(Obs(name, "crypto", "token", f"solana:{addr}", chain="solana",
                       symbol=sanitize(row.get("symbol"), 16) or None, title=sanitize(row.get("name"), 80),
                       buzz=round(1.0 + math.log1p(_f(row.get("reply_count")) or 0) / 3 + max(0, 50 - i) / 50, 4),
                       meta={"graduated": bool(row.get("complete")), "market_cap": _f(row.get("usd_market_cap")),
                             "created_ms": row.get("created_timestamp"), "rank": i + 1}))
    return out


def parse_apewisdom(name: str, domain: str, data: Any) -> list[Obs]:
    out = []
    for row in (data or {}).get("results", []) if isinstance(data, dict) else []:
        ticker = sanitize(row.get("ticker"), 16).upper()
        mentions, before = _f(row.get("mentions")) or 0.0, _f(row.get("mentions_24h_ago"))
        if not ticker or mentions <= 0:
            continue
        accel = (mentions - before) / max(before, 1.0) if before is not None else None
        out.append(Obs(name, domain, "ticker", ticker.removesuffix(".X"), symbol=ticker.removesuffix(".X"),
                       title=sanitize(row.get("name"), 80), buzz=round(math.log1p(mentions), 4),
                       sentiment=None, meta={"mentions": mentions, "mentions_24h_ago": before,
                                             "upvotes": _f(row.get("upvotes")), "accel_24h": accel,
                                             "rank": row.get("rank"), "rank_24h_ago": row.get("rank_24h_ago")}))
    return out


def parse_stocktwits(name: str, data: Any) -> list[Obs]:
    out = []
    for row in (data or {}).get("symbols", []) if isinstance(data, dict) else []:
        sym = sanitize(row.get("symbol"), 16).upper()
        if not sym:
            continue
        cls = sanitize(row.get("instrument_class"), 20)
        domain = "crypto" if cls.lower().startswith("crypto") else "stocks"
        sym = sym.removesuffix(".X")
        out.append(Obs(name, domain, "ticker", sym, symbol=sym, title=sanitize(row.get("title"), 80),
                       buzz=round(math.log1p(_f(row.get("trending_score")) or 0) + 1, 4),
                       meta={"trending_score": _f(row.get("trending_score")),
                             "summary": sanitize((row.get("trends") or {}).get("summary"), 280) or None,
                             "class": cls}))
    return out


def parse_kalshi(name: str, data: Any, *, top: int = 60) -> list[Obs]:
    rows = []
    for m in (data or {}).get("markets", []) if isinstance(data, dict) else []:
        last, prev = _f(m.get("last_price_dollars")), _f(m.get("previous_price_dollars"))
        vol = _f(m.get("volume_24h_fp")) or _f(m.get("volume_24h")) or 0.0
        ticker = sanitize(m.get("ticker"), 80)
        if not ticker or last is None or vol <= 0 or not 0 < last < 1:
            continue
        move = last - prev if prev is not None and 0 < prev < 1 else 0.0
        rows.append((abs(move) * math.log1p(vol), m, last, move, vol, ticker))
    rows.sort(key=lambda r: r[0], reverse=True)
    out = []
    for heat, m, last, move, vol, ticker in rows[:top]:
        out.append(Obs(name, "prediction", "market", f"kalshi:{ticker}", symbol=ticker,
                       title=sanitize(m.get("title"), 140), buzz=round(1 + math.log1p(vol) / 4, 4),
                       sentiment=_clip(move * 5), price=last, price_change=move, volume=vol,
                       liquidity=_f(m.get("liquidity_dollars")),
                       meta={"heat": round(heat, 4), "created": sanitize(m.get("created_time"), 40),
                             "close": sanitize(m.get("close_time"), 40)}))
    return out


def parse_polymarket(name: str, data: Any) -> list[Obs]:
    out = []
    for m in data if isinstance(data, list) else []:
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
        except (TypeError, ValueError):
            continue
        yes = _f(prices[0]) if prices else None
        mid = sanitize(m.get("id"), 40)
        if yes is None or not 0 < yes < 1 or not mid:
            continue
        vol = _f(m.get("volume24hr")) or 0.0
        move = _f(m.get("oneDayPriceChange")) or 0.0
        out.append(Obs(name, "prediction", "market", f"polymarket:{mid}", symbol=sanitize(m.get("slug"), 80),
                       title=sanitize(m.get("question"), 140), buzz=round(1 + math.log1p(vol) / 4, 4),
                       sentiment=_clip(move * 5), price=yes, price_change=move, volume=vol,
                       liquidity=_f(m.get("liquidityNum")),
                       meta={"created": sanitize(m.get("createdAt"), 40), "hour_move": _f(m.get("oneHourPriceChange"))}))
    return out


def parse_leaderboard(name: str, data: Any) -> list[Obs]:
    out = []
    for row in data if isinstance(data, list) else []:
        wallet = sanitize(row.get("proxyWallet"), 42)
        if not wallet.startswith("0x"):
            continue
        pnl = _f(row.get("pnl")) or 0.0
        out.append(Obs(name, "prediction", "trader", wallet, symbol=sanitize(row.get("userName"), 40) or wallet[:8],
                       buzz=round(math.log1p(max(pnl, 0)) / 4, 4), volume=_f(row.get("vol")),
                       meta={"pnl_day": pnl, "rank": row.get("rank")}))
    return out


def parse(source: Source, data: Any) -> list[Obs]:
    n = source.name
    if n.startswith("dexscreener"):
        return parse_dexscreener(n, data)
    if n.startswith("jupiter"):
        return parse_jupiter(n, data)
    if n.startswith("pumpfun"):
        return parse_pumpfun(n, data)
    if n.startswith("apewisdom"):
        return parse_apewisdom(n, source.domain, data)
    if n.startswith("stocktwits"):
        return parse_stocktwits(n, data)
    if n.startswith("kalshi"):
        return parse_kalshi(n, data)
    if n == "polymarket_leaderboard":
        return parse_leaderboard(n, data)
    if n.startswith("polymarket"):
        return parse_polymarket(n, data)
    return []


# -- storage and signal maths ---------------------------------------------------------------

def store(conn: sqlite3.Connection, obs: list[Obs], *, now: datetime) -> None:
    conn.executemany(
        "INSERT INTO culture_observations (at, source, domain, kind, entity, symbol, chain, title, buzz, "
        "sentiment, price, price_change, volume, liquidity, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(now.isoformat(), o.source, o.domain, o.kind, o.entity, o.symbol, o.chain, o.title, o.buzz,
          o.sentiment, o.price, o.price_change, o.volume, o.liquidity, json.dumps(o.meta or {}, default=str))
         for o in obs])


def buzz_velocity(current: float, history: list[float], prior: float | None = None) -> float:
    """Relative change of buzz vs the trailing mean.

    With no history of our own, a source-reported 24h acceleration (ApeWisdom mentions vs
    24h ago) is used when present; otherwise 0, so first sight is novelty, not velocity."""
    if not history:
        return round(_clip(prior, -3.0, 3.0), 4) if prior is not None else 0.0
    base = sum(history) / len(history)
    return round(_clip((current - base) / max(base, 0.5), -3.0, 3.0), 4)


def novelty(first_seen: datetime, now: datetime, *, half_life_h: float = 6.0) -> float:
    age_h = max(0.0, (now - first_seen).total_seconds() / 3600)
    return round(0.5 ** (age_h / half_life_h), 4)


def culture_score(buzz: float, velocity: float, sentiment: float | None, nov: float, n_sources: int) -> float:
    s = 0.35 * min(buzz / 4, 1.5) + 0.35 * max(velocity, -1) + 0.15 * nov + 0.1 * min(n_sources - 1, 3)
    if sentiment is not None:
        s += 0.15 * sentiment
    return round(s, 4)


def compute_signals(conn: sqlite3.Connection, *, now: datetime, window_h: int = 24) -> int:
    cutoff, recent = (now - timedelta(hours=window_h)).isoformat(), (now - timedelta(minutes=20)).isoformat()
    rows = conn.execute(
        "SELECT kind, entity, MAX(symbol), MAX(chain), MAX(title), MAX(domain), SUM(buzz), "
        "AVG(sentiment), COUNT(DISTINCT source), GROUP_CONCAT(DISTINCT source), MAX(price), "
        "MAX(price_change), MAX(volume), MAX(liquidity) FROM culture_observations WHERE at >= ? "
        "GROUP BY kind, entity", (recent,)).fetchall()
    for kind, ent, sym, chain, title, domain, buzz, senti, nsrc, srcs, price, pchg, vol, liq in rows:
        hist = [r[0] for r in conn.execute(
            "SELECT SUM(buzz) FROM culture_observations WHERE kind=? AND entity=? AND at>=? AND at<? "
            "GROUP BY substr(at,1,15)", (kind, ent, cutoff, recent))]
        first = conn.execute("SELECT MIN(at) FROM culture_observations WHERE kind=? AND entity=?",
                             (kind, ent)).fetchone()[0]
        prior = None
        for (meta,) in conn.execute("SELECT meta_json FROM culture_observations WHERE kind=? AND entity=? AND at>=?",
                                    (kind, ent, recent)):
            acc = _f(json.loads(meta or "{}").get("accel_24h"))
            if acc is not None:
                prior = acc if prior is None else max(prior, acc)
        vel = buzz_velocity(float(buzz), hist, prior)
        nov = novelty(datetime.fromisoformat(first), now)
        score = culture_score(float(buzz), vel, senti, nov, int(nsrc))
        conn.execute(
            "INSERT INTO culture_signals (kind, entity, symbol, chain, title, domain, buzz, buzz_velocity, "
            "sentiment, novelty, score, sources, first_seen, updated_at, price, price_change, volume, liquidity) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, entity) DO UPDATE SET "
            "symbol=excluded.symbol, chain=excluded.chain, title=excluded.title, buzz=excluded.buzz, "
            "buzz_velocity=excluded.buzz_velocity, sentiment=excluded.sentiment, novelty=excluded.novelty, "
            "score=excluded.score, sources=excluded.sources, updated_at=excluded.updated_at, "
            "price=excluded.price, price_change=excluded.price_change, volume=excluded.volume, "
            "liquidity=excluded.liquidity",
            (kind, ent, sym, chain, title, domain, round(float(buzz), 4), vel,
             None if senti is None else round(float(senti), 4), nov, score, srcs, first, now.isoformat(),
             price, pchg, vol, liq))
    return len(rows)


def culture_probability(price: float, sentiment: float | None, velocity: float) -> float:
    """Market price nudged by at most CULTURE_NUDGE in the direction of the crowd's move."""
    tilt = (sentiment or 0.0) * min(1.0, max(velocity, 0.0) + 0.25)
    return round(min(0.99, max(0.01, price + CULTURE_NUDGE * _clip(tilt))), 4)


def record_market_forecasts(conn: sqlite3.Connection, *, now: datetime, limit: int = 40) -> int:
    """One ledger forecast per hot market per 6h so accuracy can score culture vs the price."""
    from .accuracy_ledger import ensure_accuracy_schema, record_forecast

    ensure_accuracy_schema(conn)
    rows = conn.execute(
        "SELECT entity, symbol, title, price, sentiment, buzz_velocity, score, forecast_id FROM culture_signals "
        "WHERE kind='market' AND updated_at=? AND price IS NOT NULL ORDER BY score DESC LIMIT ?",
        (now.isoformat(), limit)).fetchall()
    written = 0
    for entity, symbol, title, price, senti, vel, score, _fid in rows:
        venue, market_id = entity.split(":", 1)
        recent = conn.execute(
            "SELECT 1 FROM accuracy_forecasts WHERE agent_id=? AND venue=? AND market_id=? AND recorded_at>?",
            (CULTURE_AGENT, venue, market_id, (now - timedelta(hours=6)).isoformat())).fetchone()
        if recent:
            continue
        p = culture_probability(float(price), senti, float(vel or 0))
        fid = record_forecast(
            conn, agent_id=CULTURE_AGENT, venue=venue, market_id=market_id, symbol=symbol,
            predicted_probability=p, market_price=float(price), recorded_at=now,
            decision="paper_signal" if abs(p - float(price)) >= 0.01 else "hold",
            reasoning={"source": "culture_crawler", "sentiment": senti, "buzz_velocity": vel, "score": score},
            horizon="resolution", metadata={"kind": "binary", "culture": True, "title": title})
        conn.execute("UPDATE culture_signals SET forecast_id=? WHERE kind='market' AND entity=?", (fid, entity))
        written += 1
    return written


def crawl(conn: sqlite3.Connection, client: CultureClient, *, now: datetime | None = None,
          sources: tuple[Source, ...] = SOURCES) -> dict[str, Any]:
    ensure_schema(conn)
    now = now or datetime.now(UTC)
    total = 0
    # fetch all sources in parallel (per-host serialized + spaced), then parse/store on this thread
    client.source_of.update({src.url: src.name for src in sources})
    datas = fetch_concurrently(lambda src: client.get_json(src.url), sources,
                               workers=int(os.getenv("NOEMA_CULTURE_FETCH_WORKERS", "8")))
    for src, data in zip(sources, datas, strict=True):
        obs = parse(src, data) if data is not None else []
        client.live.items(src.name, len(obs))
        store(conn, obs, now=now)
        conn.execute("INSERT INTO culture_source_state VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                     "status=excluded.status, items=excluded.items, fetched_at=excluded.fetched_at, url=excluded.url",
                     (src.name, src.domain, src.url, client.status.get(src.url, "?"), len(obs), now.isoformat()))
        total += len(obs)
        conn.commit()  # one short write txn per source instead of one long one
    signals = compute_signals(conn, now=now)
    forecasts = record_market_forecasts(conn, now=now)
    conn.execute("DELETE FROM culture_observations WHERE at < ?", ((now - timedelta(days=7)).isoformat(),))
    conn.commit()
    return {"observations": total, "signals": signals, "forecasts": forecasts}


def culture_radar(db_path: str, *, limit: int = 12) -> dict[str, Any]:
    """Read-only summary for the trading room."""
    out: dict[str, Any] = {"sources": [], "blocked": list(BLOCKED_SOURCES), "tokens": [], "tickers": [],
                           "markets": [], "traders": []}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        out["sources"] = [dict(zip(("name", "domain", "status", "items", "fetched_at"), r)) for r in conn.execute(
            "SELECT name, domain, status, items, fetched_at FROM culture_source_state ORDER BY domain, name")]
        cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        cols = ("entity", "symbol", "chain", "title", "domain", "buzz", "buzz_velocity", "sentiment",
                "novelty", "score", "sources", "price", "price_change", "volume")
        for kind, key in (("token", "tokens"), ("ticker", "tickers"), ("market", "markets"), ("trader", "traders")):
            out[key] = [dict(zip(cols, r)) for r in conn.execute(
                f"SELECT {', '.join(cols)} FROM culture_signals WHERE kind=? AND updated_at>=? "
                "ORDER BY score DESC LIMIT ?", (kind, cutoff, limit))]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="TEV read-only culture/flow crawler + alpha discovery (paper)")
    ap.add_argument("--db", default=os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=float(os.getenv("NOEMA_CULTURE_INTERVAL_SECONDS", "600")))
    args = ap.parse_args()
    from .alpha_discovery import run_cycle

    client = CultureClient()
    while True:
        conn = sqlite_connect(args.db)
        sleep_s = max(300.0, args.interval)
        try:
            client.meter = meter = SweepMeter("culture")
            client.live = LiveFeed(args.db, "culture")
            before = _signal_count(conn)
            res = crawl(conn, client)
            res["alpha"] = run_cycle(conn, client, db_path=args.db)
            fresh = _signal_count(conn) - before
            # adaptive cadence: a burst of brand-new signals/launches -> sweep again sooner
            sleep_s = adaptive_sleep(fresh, max(300.0, args.interval) / 2, floor=180.0,
                                     ceiling=max(300.0, args.interval), hot=10)
            res["sweep"] = meter.save(conn, fresh, sleep_s)
            client.meter = None
            print(json.dumps({"at": datetime.now(UTC).isoformat(), **res}, default=str), flush=True)
        except Exception as exc:  # noqa: BLE001 - crawler keeps running; logged without payloads
            print(json.dumps({"at": datetime.now(UTC).isoformat(), "error": type(exc).__name__,
                              "detail": sanitize(str(exc), 200)}), flush=True)
        finally:
            conn.close()
        if not args.loop:
            return
        time.sleep(sleep_s)


def _signal_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM culture_signals").fetchone()[0])
    except sqlite3.Error:
        return 0


if __name__ == "__main__":
    main()
