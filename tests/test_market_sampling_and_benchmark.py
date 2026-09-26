from datetime import UTC, datetime, timedelta

import httpx
import pytest

from noema.agent_config import AgentConfig
from noema.baseline_recording import record_market_baseline
from noema.config import KalshiConfig
from noema.history_forecaster import MODEL_VERSION
from noema.ledger import ForecastLedger
from noema.models import Action, Decision, Forecast, MarketSnapshot, Opportunity
from noema.outcomes import OutcomeStore
from noema.paired_evaluation import compare_history_to_market
from noema.soak import SoakStore
from noema.soak_runner import collect_rotating_market_batch
from noema.venues.kalshi import KalshiVenue


def _market(ticker: str) -> MarketSnapshot:
    return MarketSnapshot(
        "kalshi:demo", ticker, "A real question", .4, .6, .4, .6,
        1000, None, "Clear rules", captured_at=datetime.now(UTC),
    )


def test_ask_spread_does_not_fabricate_forecast_improvement(tmp_path):
    db = str(tmp_path / "noema.db")
    market = _market("KXTEST-EVENT-A")
    ledger = ForecastLedger(db)
    record_market_baseline(market, ledger)
    forecast = Forecast(market.market_id, market.venue, .5, .5, .5, MODEL_VERSION)
    ledger.append(market, forecast, Opportunity(forecast, market, .6, -.1, 0, 0, -.1),
                  Action(Decision.PASS, market.market_id, market.venue, None, 0, "test"))
    OutcomeStore(db).upsert(
        venue=market.venue, market_id=market.market_id, outcome_yes=0,
        resolved_at=(market.captured_at + timedelta(days=1)).isoformat(),
        raw={"event_ticker": "KXTEST-EVENT"},
    )
    result = compare_history_to_market(db)
    assert result.distinct_resolved_markets == 1
    assert result.candidate_brier == pytest.approx(.25)
    assert result.market_baseline_brier == pytest.approx(.25)
    assert result.brier_improvement == pytest.approx(0)


@pytest.mark.asyncio
async def test_worker_rotates_market_pages_and_resumes_after_restart(tmp_path):
    class Venue:
        name = "kalshi:demo"

        def __init__(self):
            self.requested = []

        async def market_page(self, *, cursor, limit):
            self.requested.append((cursor, limit))
            return ([_market("KXTEST-ONE-A")], "page-two") if cursor is None else (
                [_market("KXTEST-TWO-A")], None
            )

    db = str(tmp_path / "noema.db")
    venue = Venue()
    for _ in range(3):
        await collect_rotating_market_batch(venue, SoakStore(db), max_markets=1)
    assert venue.requested == [(None, 1), ("page-two", 1), (None, 1)]
    assert SoakStore(db).stats().distinct_markets == 2


@pytest.mark.asyncio
async def test_failed_market_page_preserves_cursor(tmp_path):
    class Venue:
        name = "kalshi:demo"

        async def market_page(self, *, cursor, limit):
            if cursor:
                raise httpx.ConnectError("network offline")
            return [_market("KXTEST-ONE-A")], "page-two"

    store = SoakStore(str(tmp_path / "noema.db"))
    venue = Venue()
    await collect_rotating_market_batch(venue, store, max_markets=1)
    with pytest.raises(httpx.ConnectError):
        await collect_rotating_market_batch(venue, store, max_markets=1)
    assert store.scan_cursor("kalshi:demo:markets") == "page-two"


@pytest.mark.asyncio
async def test_expired_cursor_restarts_once(tmp_path):
    class Venue:
        name = "kalshi:demo"

        def __init__(self):
            self.requested = []

        async def market_page(self, *, cursor, limit):
            self.requested.append(cursor)
            if cursor:
                request = httpx.Request("GET", "https://example.test/markets")
                raise httpx.HTTPStatusError(
                    "cursor expired", request=request,
                    response=httpx.Response(400, request=request),
                )
            return [_market("KXTEST-ONE-A")], "new-page"

    store = SoakStore(str(tmp_path / "noema.db"))
    store.set_scan_cursor("kalshi:demo:markets", "expired")
    venue = Venue()
    await collect_rotating_market_batch(venue, store, max_markets=1)
    assert venue.requested == ["expired", None]
    assert store.scan_cursor("kalshi:demo:markets") == "new-page"


@pytest.mark.asyncio
async def test_kalshi_page_uses_official_cursor_and_limit():
    captured = []

    def handler(request):
        captured.append(dict(request.url.params))
        return httpx.Response(200, json={"markets": [{"ticker": "KXTEST-A"}],
                                         "cursor": "next-page"})

    client = httpx.AsyncClient(base_url="https://example.test",
                               transport=httpx.MockTransport(handler))
    venue = KalshiVenue(config=KalshiConfig(), client=client)
    try:
        markets, cursor = await venue.market_page(cursor="previous", limit=25)
    finally:
        await venue.close()
    assert [market.market_id for market in markets] == ["KXTEST-A"]
    assert cursor == "next-page"
    assert captured == [{"status": "open", "limit": "25", "mve_filter": "exclude",
                         "cursor": "previous"}]


@pytest.mark.asyncio
async def test_malformed_page_cannot_advance_cursor():
    client = httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"markets": [{"title": "missing ticker"}], "cursor": "next"},
        )),
    )
    venue = KalshiVenue(config=KalshiConfig(), client=client)
    try:
        with pytest.raises(ValueError, match="ticker"):
            await venue.market_page(cursor="previous", limit=25)
    finally:
        await venue.close()


def test_agent_market_limit_matches_exchange_limit():
    with pytest.raises(ValueError, match="1 and 1000"):
        AgentConfig(max_markets_per_cycle=1001).validate()
