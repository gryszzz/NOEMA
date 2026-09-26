import httpx
import pytest

from noema.config import KalshiConfig
from noema.venues.kalshi import KalshiVenue


@pytest.mark.asyncio
async def test_public_event_membership_checks_ticker_and_nested_markets() -> None:
    def respond(request):
        assert request.url.path.endswith("/events/KXTEST-EVENT")
        assert request.url.params["with_nested_markets"] == "true"
        return httpx.Response(200, json={
            "event": {"event_ticker": "KXTEST-EVENT", "markets": [
                {"ticker": "KXTEST-EVENT-A"}, {"ticker": "KXTEST-EVENT-B"},
            ]},
        })

    client = httpx.AsyncClient(
        base_url=KalshiConfig().base_url, transport=httpx.MockTransport(respond)
    )
    venue = KalshiVenue(KalshiConfig(), client=client)
    assert await venue.event_market_tickers("KXTEST-EVENT") == {
        "KXTEST-EVENT-A", "KXTEST-EVENT-B",
    }
    with pytest.raises(ValueError):
        await venue.event_market_tickers("../private")
    await venue.close()
