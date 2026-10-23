import json

import httpx
import pytest

from noema import public_quotes as pq


@pytest.fixture(autouse=True)
def _nogap(monkeypatch):
    monkeypatch.setattr(pq, "MIN_GAP_S", {"kalshi": 0, "gamma": 0})


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_kalshi_quote_cached():
    calls = []

    def h(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"market": {"yes_bid_dollars": "0.59", "yes_ask_dollars": "0.63",
                                                    "last_price_dollars": "0.6", "status": "active"}})
    q = pq.PublicQuotes(_client(h))
    a = q.kalshi("KXBOND-30-JAC")
    assert a.yes_mid == pytest.approx(0.61) and a.side_price("NO") == pytest.approx(0.39)
    q.kalshi("KXBOND-30-JAC")
    assert len(calls) == 1


def test_429_pauses_host_and_budget_limits_calls():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"})
    q = pq.PublicQuotes(_client(h))
    assert q.kalshi("A-1") is None and q.kalshi("B-2") is None
    assert len(calls) == 1
    q2 = pq.PublicQuotes(_client(lambda r: httpx.Response(200, json={"market": {}})), max_calls=2)
    for t in ("A-1", "B-2", "C-3", "D-4"):
        q2.kalshi(t)
    assert q2.calls == 2


def _gm(slug, question, bid=0.37, ask=0.38, closed=False, prices=("0.5", "0.5")):
    return {"slug": slug, "question": question, "bestBid": bid, "bestAsk": ask, "lastTradePrice": 0.375,
            "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps(list(prices)), "closed": closed}


def test_match_is_strict_taipei_not_new_taipei():
    ev = {"events": [{"markets": [_gm("puma-new-taipei", "Will Puma Shen win the New Taipei mayor election?"),
                                  _gm("puma-taipei", "Will Puma Shen win the next Taipei mayor election?")]}]}
    q = pq.PublicQuotes(_client(lambda r: httpx.Response(200, json=ev)))
    assert q.match_polymarket("Taipei mayor 2026: Puma Shen wins") == "puma-taipei"


def test_gamma_settles_and_ignores_placeholder_book():
    q = pq.PublicQuotes._gamma_quote(_gm("s", "q", 0, 1, closed=True, prices=("1", "0")))
    assert q.result == "yes" and q.bid is None and q.side_price("NO") == 0.0
    q2 = pq.PublicQuotes._gamma_quote(_gm("s", "q", 0, 1))
    assert q2.result is None and q2.bid is None


def test_numeric_polymarket_id_uses_markets_path():
    seen = []

    def h(req):
        seen.append(req.url.path)
        return httpx.Response(200, json=_gm("maine-gop", "Maine?"))
    q = pq.PublicQuotes(_client(h))
    assert q.quote("polymarket", "630773").market_id == "maine-gop"
    assert seen == ["/markets/630773"]
