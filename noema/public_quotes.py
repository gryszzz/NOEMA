"""Read-only public quotes for paper positions (no keys, no auth, never trades).

* Kalshi: ``GET /trade-api/v2/markets/{ticker}`` on the public elections host (bid/ask/last, status, result).
* Polymarket international: Gamma ``/markets?slug=`` (bestBid/bestAsk/lastTradePrice, closed, outcomePrices).
  Polymarket international slugs differ from polymarket-us ones, so a pick without a usable slug is matched
  by Gamma ``/public-search`` against the pick's market text (strict token match, unique best, else unmatched).

Calls are light: per-market results are cached (``NOEMA_PUBLIC_QUOTE_TTL_S``, default 60 s), at most
``NOEMA_PUBLIC_QUOTE_MAX_CALLS`` (default 30) network calls per process per cycle, a minimum gap between calls
per host, and a host is paused after HTTP 429/5xx (Retry-After honoured). Every error degrades to "no quote".
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

KALSHI = os.getenv("NOEMA_KALSHI_PUBLIC_BASE", "https://api.elections.kalshi.com/trade-api/v2")
GAMMA = os.getenv("NOEMA_GAMMA_BASE", "https://gamma-api.polymarket.com")
TTL_S = float(os.getenv("NOEMA_PUBLIC_QUOTE_TTL_S", "60"))
MAX_CALLS = int(os.getenv("NOEMA_PUBLIC_QUOTE_MAX_CALLS", "30"))
MIN_GAP_S = {"kalshi": 0.25, "gamma": 0.4}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,200}$")
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,80}$")
_STOP = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "will", "be", "by", "at", "is", "next",
         "who", "what", "which", "win", "wins", "won", "winner", "election", "race", "2025", "2026", "2027", "2028"}


@dataclass
class Quote:
    venue: str
    market_id: str
    bid: float | None
    ask: float | None
    last: float | None
    result: str | None  # 'yes' | 'no' when officially resolved
    status: str
    title: str | None
    at: float
    source: str

    @property
    def yes_mid(self) -> float | None:
        if self.result in ("yes", "no"):
            return 1.0 if self.result == "yes" else 0.0
        if self.bid is not None and self.ask is not None and 0 < self.ask <= 1 and self.ask >= self.bid:
            return round((self.bid + self.ask) / 2, 4)
        return self.last

    def side_price(self, side: str) -> float | None:
        m = self.yes_mid
        if m is None:
            return None
        return m if side.upper() == "YES" else round(1 - m, 4)


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if 0 <= x <= 1 else None


def _tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        w = w[:-1] if len(w) > 4 and w.endswith("s") else w
        if len(w) >= 3 and w not in _STOP:
            out.add(w)
    return out


class PublicQuotes:
    """Per-process cached, rate-limited public quote client."""

    def __init__(self, client: httpx.Client | None = None, *, ttl_s: float = TTL_S, max_calls: int = MAX_CALLS):
        self._client = client
        self.ttl_s = ttl_s
        self.max_calls = max_calls
        self.calls = 0
        self.errors: dict[str, int] = {}
        self._cache: dict[tuple[str, str], tuple[float, Quote | None]] = {}
        self._search: dict[str, tuple[float, str | None]] = {}
        self._last: dict[str, float] = {}
        self._paused: dict[str, float] = {}
        self._lock = threading.Lock()

    def new_cycle(self) -> None:
        self.calls = 0

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0, headers={"User-Agent": "noema-paper-marks/1"})
        return self._client

    def _get(self, host: str, url: str, params: dict[str, Any] | None = None) -> Any:
        now = time.monotonic()
        if self.calls >= self.max_calls or self._paused.get(host, 0) > now:
            return None
        gap = MIN_GAP_S.get(host, 0.3) - (now - self._last.get(host, -1e9))
        if gap > 0:
            time.sleep(gap)
        self.calls += 1
        self._last[host] = time.monotonic()
        try:
            r = self.client.get(url, params=params)
        except httpx.HTTPError as exc:
            self.errors[type(exc).__name__] = self.errors.get(type(exc).__name__, 0) + 1
            return None
        if r.status_code == 429 or r.status_code >= 500:
            try:
                wait = float(r.headers.get("Retry-After", "30"))
            except ValueError:
                wait = 30.0
            self._paused[host] = time.monotonic() + min(300.0, max(5.0, wait))
            self.errors[str(r.status_code)] = self.errors.get(str(r.status_code), 0) + 1
            return None
        if r.status_code != 200:
            self.errors[str(r.status_code)] = self.errors.get(str(r.status_code), 0) + 1
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def _cached(self, key: tuple[str, str]) -> tuple[bool, Quote | None]:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return True, hit[1]
        return False, None

    def kalshi(self, ticker: str) -> Quote | None:
        ticker = (ticker or "").strip().upper()
        if not _TICKER.match(ticker):
            return None
        ok, q = self._cached(("kalshi", ticker))
        if ok:
            return q
        d = self._get("kalshi", f"{KALSHI}/markets/{ticker}")
        if d is None:
            return self._cache.get(("kalshi", ticker), (0, None))[1]
        m = d.get("market") if isinstance(d, dict) else None
        q = None
        if isinstance(m, dict):
            res = str(m.get("result") or "").lower()
            settled = m.get("status") in ("finalized", "settled", "determined") and res in ("yes", "no")
            q = Quote("kalshi", ticker, _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars")),
                      _f(m.get("last_price_dollars")), res if settled else None, str(m.get("status") or ""),
                      str(m.get("title") or "")[:200], time.time(), "kalshi public")
        self._cache[("kalshi", ticker)] = (time.monotonic(), q)
        return q

    @staticmethod
    def _gamma_quote(m: dict[str, Any]) -> Quote | None:
        slug = str(m.get("slug") or "")
        if not slug:
            return None
        try:
            outcomes = [str(x).lower() for x in json.loads(m.get("outcomes") or "[]")]
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except (TypeError, ValueError):
            outcomes, prices = [], []
        result = None
        resolved = m.get("umaResolutionStatus") in ("resolved", None) and bool(m.get("closed"))
        if resolved and outcomes[:2] == ["yes", "no"] and len(prices) >= 2 and sorted(prices[:2]) == [0.0, 1.0]:
            result = "yes" if prices[0] == 1.0 else "no"
        bid, ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
        if bid is not None and ask is not None and ask - bid >= 0.99:
            bid = ask = None  # placeholder book (0/1): no real quote
        return Quote("polymarket", slug, bid, ask, _f(m.get("lastTradePrice")), result,
                     "closed" if m.get("closed") else "active", str(m.get("question") or "")[:200], time.time(),
                     "polymarket gamma")

    def polymarket(self, slug: str) -> Quote | None:
        slug = (slug or "").strip().lower()
        if slug.isdigit() and len(slug) <= 12:
            return self._polymarket_id(slug)
        if not _SLUG.match(slug):
            return None
        ok, q = self._cached(("polymarket", slug))
        if ok:
            return q
        d = self._get("gamma", f"{GAMMA}/markets", {"slug": slug})
        if d is None:
            return self._cache.get(("polymarket", slug), (0, None))[1]
        q = self._gamma_quote(d[0]) if isinstance(d, list) and d and isinstance(d[0], dict) else None
        self._cache[("polymarket", slug)] = (time.monotonic(), q)
        return q

    def _polymarket_id(self, mid: str) -> Quote | None:
        """Gamma numeric market id (what the polymarket.com market page / API calls `id`)."""
        ok, q = self._cached(("polymarket-id", mid))
        if ok:
            return q
        d = self._get("gamma", f"{GAMMA}/markets/{mid}", None)
        if d is None:
            return self._cache.get(("polymarket-id", mid), (0, None))[1]
        q = self._gamma_quote(d) if isinstance(d, dict) else None
        self._cache[("polymarket-id", mid)] = (time.monotonic(), q)
        return q

    def match_polymarket(self, text: str) -> str | None:
        """Gamma slug for a pick's market text: every name token must hit, best Jaccard must be unique."""
        want = _tokens(text)
        if len(want) < 2:
            return None
        key = " ".join(sorted(want))
        hit = self._search.get(key)
        if hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
        d = self._get("gamma", f"{GAMMA}/public-search", {"q": text[:120], "limit_per_type": 8})
        if d is None:
            return None
        scored: list[tuple[float, str]] = []
        for e in (d.get("events") or []) if isinstance(d, dict) else []:
            for m in e.get("markets") or []:
                have = _tokens(str(m.get("question") or ""))
                if not have or len(want & have) / len(want) < 0.75:
                    continue
                scored.append((len(want & have) / len(want | have), str(m.get("slug") or "")))
        scored.sort(reverse=True)
        slug = None
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]) and scored[0][1]:
            slug = scored[0][1]
            q = self._gamma_quote(next(m for e in d.get("events") or [] for m in e.get("markets") or []
                                       if m.get("slug") == slug))
            self._cache[("polymarket", slug)] = (time.monotonic(), q)
        self._search[key] = (time.monotonic(), slug)
        return slug

    def quote(self, venue: str, market_id: str | None, text: str = "") -> Quote | None:
        """Quote for a paper position. venue: kalshi* | polymarket | polymarket-us (international fallback)."""
        v = (venue or "").lower()
        if v.startswith("kalshi"):
            return self.kalshi(market_id or "")
        if v.startswith("polymarket"):
            q = self.polymarket(market_id) if market_id else None
            if q is None and text:
                slug = self.match_polymarket(text)
                q = self.polymarket(slug) if slug else None
            return q
        return None


_SHARED: PublicQuotes | None = None


def shared() -> PublicQuotes:
    global _SHARED
    if _SHARED is None:
        _SHARED = PublicQuotes()
    return _SHARED
