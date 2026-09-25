from noema.config import KalshiConfig
from noema.venues.kalshi_stream import KalshiStream


def test_demo_stream_url_without_connecting(tmp_path) -> None:
    # Constructor requires a key file, so use a minimal object without invoking it.
    stream = object.__new__(KalshiStream)
    stream.config = KalshiConfig(environment="demo")
    assert "external-api-ws.demo.kalshi.co" in stream.url


def test_production_stream_url_without_connecting() -> None:
    stream = object.__new__(KalshiStream)
    stream.config = KalshiConfig(environment="production")
    assert stream.url == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
