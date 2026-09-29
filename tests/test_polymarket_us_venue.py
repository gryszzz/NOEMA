from __future__ import annotations

import asyncio

import pytest

from noema.prediction_venues import _count_records, _normalize_polymarket_depth
from noema.venues.polymarket_us import PolymarketUSVenue


class FakeMarkets:
    def list(self, params):
        assert params["limit"] == 1
        return {"markets": [{
            "slug": "us-market-one", "title": "Candidate", "question": "Election",
            "active": True, "closed": False, "endDate": "2026-12-31T00:00:00Z",
            "description": "Official settlement text", "outcomes": ["Yes", "No"],
        }]}

    def bbo(self, slug):
        assert slug == "us-market-one"
        return {"marketData": {
            "marketSlug": slug,
            "bestBid": {"value": "0.41", "currency": "USD"},
            "bestAsk": {"value": "0.44", "currency": "USD"},
            "bidDepth": 20, "askDepth": 10,
        }}

    def book(self, slug):
        return {"marketData": {"marketSlug": slug, "bids": [], "offers": []}}

    def settlement(self, slug):
        return {"slug": slug, "settlement": 1}


class FakeClient:
    markets = FakeMarkets()
    def close(self): pass


def test_public_polymarket_market_snapshot_keeps_unknown_depth_unknown():
    venue = PolymarketUSVenue(FakeClient())
    markets, cursor = asyncio.run(venue.market_page(limit=1))
    market = markets[0]
    assert cursor == "1"
    assert market.venue == "polymarket-us"
    assert market.market_id == "us-market-one"
    assert market.yes_bid == .41 and market.yes_ask == .44
    assert market.no_bid == .56 and market.no_ask == pytest.approx(.59)
    assert market.liquidity_usd is None
    assert market.resolution_rules == "Official settlement text"


def test_invalid_polymarket_cursor_fails_closed():
    venue = PolymarketUSVenue(FakeClient())
    try:
        asyncio.run(venue.market_page(cursor="next", limit=1))
    except ValueError as exc:
        assert "cursor" in str(exc)
    else:
        raise AssertionError("invalid cursor must fail closed")


def test_polymarket_positions_count_preserves_empty_and_unknown():
    assert _count_records({"positions": {}}, "positions") == 0
    assert _count_records({"positions": []}, "positions") == 0
    assert _count_records({"unexpected": []}, "positions") is None


def test_polymarket_l2_depth_requires_exact_market_and_sorted_positive_contract_levels():
    raw = {"marketData": {
        "marketSlug": "fixed-market",
        "bids": [{"px": {"value": "0.42"}, "qty": "2"}],
        "offers": [{"px": {"value": "0.44"}, "qty": "1.5"}],
    }}
    normalized = _normalize_polymarket_depth(raw, "fixed-market")
    assert normalized["yes_bids"] == [{"price": "0.42", "contracts": "2"}]
    assert normalized["yes_asks"] == [{"price": "0.44", "contracts": "1.5"}]
    assert "capacity" not in normalized

    assert _normalize_polymarket_depth(raw, "other-market") is None
    malformed = {"marketData": {**raw["marketData"],
                "offers": [{"px": {"value": "0.45"}, "qty": "1"},
                           {"px": {"value": "0.44"}, "qty": "2"}]}}
    assert _normalize_polymarket_depth(malformed, "fixed-market") is None
