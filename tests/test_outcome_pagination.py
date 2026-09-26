from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from noema.config import KalshiConfig
from noema.history_forecaster import MODEL_VERSION
from noema.outcomes import OutcomeStore
from noema.paper_execution import FeeTerms, quote_yes_taker
from noema.paper_research import Candidate, PaperResearchStore
from noema.sync import sync_kalshi_outcomes
from noema.venues.kalshi_history import KalshiHistory


def _settled(ticker):
    return {"ticker": ticker, "result": "yes", "settlement_ts": "2026-09-25T12:00:00Z",
            "event_ticker": ticker.rsplit("-", 1)[0]}


@pytest.mark.asyncio
async def test_bounded_sync_resumes_both_partitions_after_restart(tmp_path):
    class History:
        config = KalshiConfig(environment="demo")

        def __init__(self):
            self.calls = []

        async def settled_page(self, partition, *, cursor, limit):
            self.calls.append((partition, cursor, limit))
            if cursor is None:
                return [_settled(f"KXTEST-{partition}-A")], f"{partition}-next"
            return [_settled(f"KXTEST-{partition}-B")], None

    db = str(tmp_path / "noema.db")
    history = History()
    for _ in range(2):
        await sync_kalshi_outcomes(OutcomeStore(db), history, max_markets=2)
    assert history.calls == [
        ("live", None, 1), ("historical", None, 1),
        ("live", "live-next", 1), ("historical", "historical-next", 1),
    ]
    assert OutcomeStore(db).conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 4


@pytest.mark.asyncio
async def test_small_budget_alternates_partitions_and_network_failure_keeps_cursor(tmp_path):
    class History:
        config = KalshiConfig(environment="demo")

        def __init__(self):
            self.calls = []
            self.fail = False

        async def settled_page(self, partition, *, cursor, limit):
            self.calls.append((partition, cursor))
            if self.fail:
                raise httpx.ConnectError("offline")
            return [_settled(f"KXTEST-{partition}-A")], "next-page"

    history = History()
    store = OutcomeStore(str(tmp_path / "noema.db"))
    await sync_kalshi_outcomes(store, history, max_markets=1)
    await sync_kalshi_outcomes(store, history, max_markets=1)
    history.fail = True
    with pytest.raises(httpx.ConnectError):
        await sync_kalshi_outcomes(store, history, max_markets=1)
    assert history.calls == [("live", None), ("historical", None), ("live", "next-page")]
    assert store.scan_cursor("kalshi:demo:settlements:live") == "next-page"


@pytest.mark.asyncio
async def test_expired_settlement_cursor_restarts_once(tmp_path):
    class History:
        config = KalshiConfig(environment="demo")

        def __init__(self):
            self.calls = []

        async def settled_page(self, partition, *, cursor, limit):
            self.calls.append(cursor)
            if cursor is not None:
                request = httpx.Request("GET", "https://example.test/markets")
                raise httpx.HTTPStatusError(
                    "cursor expired", request=request,
                    response=httpx.Response(400, request=request),
                )
            return [_settled("KXTEST-NEW-A")], "next"

    store = OutcomeStore(str(tmp_path / "noema.db"))
    store.set_scan_cursor("kalshi:demo:settlements:live", "expired")
    history = History()
    await sync_kalshi_outcomes(store, history, max_markets=1)
    assert history.calls == ["expired", None]
    assert store.scan_cursor("kalshi:demo:settlements:live") == "next"


@pytest.mark.asyncio
async def test_history_pages_use_official_partitions_and_cursor():
    captured = []

    def handler(request):
        captured.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"markets": [_settled("KXTEST-ONE-A")],
                                         "cursor": "next"})

    client = httpx.AsyncClient(base_url="https://example.test",
                               transport=httpx.MockTransport(handler))
    history = KalshiHistory(config=KalshiConfig(), client=client)
    try:
        await history.settled_page("live", cursor=None, limit=25)
        await history.settled_page("historical", cursor="old", limit=25)
    finally:
        await history.close()
    assert captured == [
        ("/markets", {"limit": "25", "mve_filter": "exclude", "status": "settled"}),
        ("/historical/markets", {"limit": "25", "mve_filter": "exclude", "cursor": "old"}),
    ]


@pytest.mark.asyncio
async def test_pending_paper_quote_is_resolved_before_broad_scan(tmp_path):
    db = str(tmp_path / "noema.db")
    paper = PaperResearchStore(db)
    ticker = "KXTEST-FUTURE-A"
    now = datetime.now(UTC)
    candidate = Candidate("kalshi:demo", ticker, MODEL_VERSION, .9, .8, .9,
                          now - timedelta(seconds=2))
    terms = FeeTerms("quadratic", Decimal(1))
    quote = quote_yes_taker(
        {"orderbook_fp": {"yes_dollars": [["0.49", "2.00"]],
                          "no_dollars": [["0.50", "2.00"]]}},
        contracts=Decimal(1), probability_yes=.9, fee_terms=terms,
    )
    paper.record(candidate, now - timedelta(seconds=1), quote, terms, selected=True)

    class History:
        config = KalshiConfig(environment="demo")

        async def market_by_ticker(self, requested):
            assert requested == ticker
            return {**_settled(ticker),
                    "settlement_ts": (now + timedelta(minutes=1)).isoformat()}

        async def settled_page(self, partition, *, cursor, limit):
            return [], None

    store = OutcomeStore(db)
    result = await sync_kalshi_outcomes(store, History(), max_markets=20)
    assert (result.scanned, result.imported) == (1, 1)
    assert paper.audit()["market_count"] == 1
    assert store.pending_paper_quotes("kalshi:demo", limit=5) == []


@pytest.mark.asyncio
async def test_individual_settlement_uses_historical_endpoint_after_404():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path.startswith("/historical/"):
            return httpx.Response(200, json={"market": _settled("KXTEST-OLD-A")})
        return httpx.Response(404)

    client = httpx.AsyncClient(base_url="https://example.test",
                               transport=httpx.MockTransport(handler))
    history = KalshiHistory(config=KalshiConfig(), client=client)
    try:
        market = await history.market_by_ticker("KXTEST-OLD-A")
    finally:
        await history.close()
    assert market is not None and market["ticker"] == "KXTEST-OLD-A"
    assert requested == ["/markets/KXTEST-OLD-A", "/historical/markets/KXTEST-OLD-A"]
