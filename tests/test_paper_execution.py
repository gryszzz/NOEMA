from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from noema.config import KalshiConfig
from noema.engine import CostModel, NoemaEngine
from noema.history_forecaster import MODEL_VERSION
from noema.ledger import ForecastLedger
from noema.models import Action, Decision, Forecast, MarketSnapshot, Mode, Opportunity
from noema.outcomes import OutcomeStore
from noema.paper_execution import FeeTerms, quote_yes_taker, taker_fee
from noema.paper_research import PaperResearchStore, collect_paper_quote
from noema.risk import RiskEngine, RiskPolicy
from noema.venues.kalshi import KalshiVenue


def test_kalshi_fee_schedule_and_event_override():
    terms = FeeTerms.from_api(
        {"fee_type": "quadratic", "fee_multiplier": 1},
        {"fee_type_override": None, "fee_multiplier_override": 2},
    )
    assert taker_fee(Decimal("0.5"), Decimal(1), terms) == Decimal("0.04")
    assert taker_fee(Decimal("0.5"), Decimal(100), FeeTerms("quadratic", Decimal(1))) == (
        Decimal("1.75")
    )
    with pytest.raises(ValueError, match="missing"):
        FeeTerms.from_api({"fee_type": "quadratic"}, {})
    with pytest.raises(ValueError, match="unsupported"):
        FeeTerms.from_api({"fee_type": "flat", "fee_multiplier": 1}, {})


@pytest.mark.asyncio
async def test_fee_lookup_applies_event_override_only_for_matching_tickers():
    def handler(request):
        if request.url.path.endswith("/series/KXTEST"):
            return httpx.Response(200, json={"series": {
                "ticker": "KXTEST", "fee_type": "quadratic", "fee_multiplier": 1,
            }})
        return httpx.Response(200, json={"event": {
            "event_ticker": "KXTEST-EVENT", "fee_multiplier_override": 0,
        }})

    client = httpx.AsyncClient(base_url="https://example.test",
                               transport=httpx.MockTransport(handler))
    venue = KalshiVenue(config=KalshiConfig(), client=client)
    try:
        terms = await venue.taker_fee_terms("KXTEST-EVENT-A")
        assert terms.taker_multiplier == 0
        assert taker_fee(Decimal("0.5"), Decimal(1), terms) == 0
        with pytest.raises(ValueError, match="invalid market ticker"):
            await venue.taker_fee_terms("../other")
    finally:
        await venue.close()


@pytest.mark.asyncio
async def test_public_top_of_book_supports_small_quotes_without_credentials():
    response = {"market": {
        "ticker": "KXTEST-EVENT-A", "status": "open",
        "yes_bid_dollars": "0.4900", "yes_bid_size_fp": "4.00",
        "yes_ask_dollars": "0.5000", "yes_ask_size_fp": "1.00",
    }}

    def handler(request):
        assert request.url.path == "/markets/KXTEST-EVENT-A"
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(base_url="https://example.test",
                               transport=httpx.MockTransport(handler))
    venue = KalshiVenue(config=KalshiConfig(), client=client)
    try:
        book, source = await venue.paper_book("KXTEST-EVENT-A")
        assert source == "public_top_of_book"
        quote = quote_yes_taker(book, contracts=Decimal(1), probability_yes=.8,
                                fee_terms=FeeTerms("quadratic", Decimal(1)),
                                quote_source=source)
        assert quote.average_yes_price == Decimal("0.5000")
        with pytest.raises(ValueError, match="insufficient visible"):
            quote_yes_taker(book, contracts=Decimal(2), probability_yes=.8,
                            fee_terms=FeeTerms("quadratic", Decimal(1)))
        response["market"].pop("yes_ask_size_fp")
        with pytest.raises(ValueError, match="missing valid public"):
            await venue.paper_book("KXTEST-EVENT-A")
    finally:
        await venue.close()


def test_quote_walks_no_bids_for_yes_asks_and_requires_full_visible_depth():
    book = {"orderbook_fp": {"yes_dollars": [["0.49", "10.00"]], "no_dollars": [
        ["0.40", "1.00"], ["0.50", "1.00"],
    ]}}
    terms = FeeTerms("quadratic", Decimal(1))
    quote = quote_yes_taker(book, contracts=Decimal(2), probability_yes=.8, fee_terms=terms)
    assert quote.best_yes_ask == Decimal("0.50")
    assert quote.entry_cost_usd == Decimal("1.10")
    assert quote.taker_fee_usd == Decimal("0.04")
    assert quote.expected_net_pnl_usd == Decimal("0.46")
    with pytest.raises(ValueError, match="insufficient visible"):
        quote_yes_taker(book, contracts=Decimal(3), probability_yes=.8, fee_terms=terms)
    with pytest.raises(ValueError, match="non-finite"):
        quote_yes_taker({"orderbook_fp": {"yes_dollars": [["0.49", "10"]],
                                             "no_dollars": [["nan", "10"]]}},
                        contracts=Decimal(1), probability_yes=.8, fee_terms=terms)
    with pytest.raises(ValueError, match="spread outside"):
        quote_yes_taker({"orderbook_fp": {"yes_dollars": [["0.30", "10"]],
                                             "no_dollars": [["0.50", "10"]]}},
                        contracts=Decimal(1), probability_yes=.8, fee_terms=terms)


def _record_candidate(db: str, *, ticker: str, p: float, lower: float) -> None:
    snapshot = MarketSnapshot(
        "kalshi:demo", ticker, "Clear question", .45, .5, .5, .55,
        1000, None, "Clear rules", captured_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    forecast = Forecast(ticker, snapshot.venue, p, lower, p, MODEL_VERSION)
    ForecastLedger(db).append(
        snapshot, forecast,
        Opportunity(forecast, snapshot, .5, p - .5, .04, (p - lower) / 2, 0),
        Action(Decision.PASS, ticker, snapshot.venue, None, 0, "research"),
    )


@pytest.mark.asyncio
async def test_first_quote_is_immutable_and_audit_uses_only_later_settlement(tmp_path):
    db = str(tmp_path / "paper.db")
    ticker = "KXTEST-EVENT-A"
    _record_candidate(db, ticker=ticker, p=.9, lower=.8)

    class Venue:
        name = "kalshi:demo"

        async def taker_fee_terms(self, market_ticker):
            assert market_ticker == ticker
            return FeeTerms("quadratic", Decimal(1))

        async def paper_book(self, market_ticker):
            assert market_ticker == ticker
            return {"orderbook_fp": {"yes_dollars": [["0.49", "2.00"]],
                                     "no_dollars": [["0.50", "1.00"]]}}, "public_top_of_book"

    store = PaperResearchStore(db)
    first = await collect_paper_quote(Venue(), store, ticker)
    assert first.status == "paper_candidate"
    assert first.quote is not None
    assert first.quote.total_debit_usd == Decimal("0.52")
    assert first.quote.quote_source == "public_top_of_book"
    assert (await collect_paper_quote(Venue(), store, ticker)).status == "already_recorded"
    assert store.audit()["status"] == "no_settled_paper_quotes"

    later = datetime.now(UTC) + timedelta(minutes=1)
    OutcomeStore(db).upsert(
        venue="kalshi:demo", market_id=ticker, outcome_yes=1,
        resolved_at=later.isoformat(), seen_at=later + timedelta(seconds=1),
        raw={"event_ticker": "KXTEST-EVENT"},
    )
    report = store.audit()
    assert report["status"] == "research_only"
    assert report["observed_quote_count"] == report["selected_quote_count"] == 1
    assert report["market_count"] == report["event_count"] == 1
    assert Decimal(report["net_pnl_usd"]) == Decimal("0.48")
    assert report["live_eligible"] is False


@pytest.mark.asyncio
async def test_missing_forecast_and_stale_forecast_fail_closed(tmp_path):
    db = str(tmp_path / "paper.db")
    ticker = "KXTEST-EVENT-A"
    class Venue:
        name = "kalshi:demo"

    store = PaperResearchStore(db)
    assert (await collect_paper_quote(Venue(), store, ticker)).status == "unavailable"
    _record_candidate(db, ticker=ticker, p=.9, lower=.8)
    result = await collect_paper_quote(Venue(), store, ticker, max_forecast_age_seconds=0)
    assert result.detail == "independent forecast is stale"


@pytest.mark.asyncio
async def test_oversized_paper_quote_is_recorded_as_pass(tmp_path):
    db = str(tmp_path / "paper.db")
    ticker = "KXTEST-EVENT-A"
    _record_candidate(db, ticker=ticker, p=.95, lower=.9)

    class Venue:
        name = "kalshi:demo"

        async def taker_fee_terms(self, market_ticker):
            return FeeTerms("quadratic", Decimal(1))

        async def paper_book(self, market_ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.49", "100.00"]],
                                     "no_dollars": [["0.50", "100.00"]]}}, "public_top_of_book"

    store = PaperResearchStore(db)
    result = await collect_paper_quote(Venue(), store, ticker, contracts=Decimal(25))
    assert result.status == "paper_pass"
    assert store.audit()["selected_quote_count"] == 0


def test_prototype_cost_does_not_double_count_spread_and_live_kalshi_is_blocked(tmp_path):
    snapshot = MarketSnapshot("kalshi:demo", "TICKER", "Clear", .4, .6, .4, .6,
                              1000, None, "Clear")
    assert CostModel().estimate(snapshot) == pytest.approx(.04)

    class Venue:
        name = "kalshi:production"

    with pytest.raises(RuntimeError, match="verified fee and depth"):
        NoemaEngine(Venue(), object(), RiskEngine(RiskPolicy(mode=Mode.LIVE)),
                    ForecastLedger(str(tmp_path / "ledger.db")))
