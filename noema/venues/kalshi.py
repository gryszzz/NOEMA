from __future__ import annotations

import base64
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noema.config import KalshiConfig
from noema.models import Action, Decision, MarketSnapshot
from noema.paper_execution import FeeTerms
from noema.venues.base import VenueAdapter


def _decimal(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(Decimal(str(value)))


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _estimated_top_book_liquidity(market: dict[str, Any]) -> float | None:
    yes_bid = _decimal(market.get("yes_bid_dollars"))
    yes_ask = _decimal(market.get("yes_ask_dollars"))
    bid_size = _decimal(market.get("yes_bid_size_fp"))
    ask_size = _decimal(market.get("yes_ask_size_fp"))
    if None in {yes_bid, yes_ask, bid_size, ask_size}:
        return None
    assert yes_bid is not None and yes_ask is not None
    assert bid_size is not None and ask_size is not None
    return yes_bid * bid_size + yes_ask * ask_size


class KalshiSigner:
    def __init__(self, key_id: str, private_key_path: str) -> None:
        self.key_id = key_id
        pem = Path(private_key_path).read_bytes()
        self.private_key = serialization.load_pem_private_key(pem, password=None)

    def headers(self, method: str, request_path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        path_without_query = request_path.split("?", 1)[0]
        message = (timestamp + method.upper() + path_without_query).encode()

        if isinstance(self.private_key, Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            signature = self.private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }


class KalshiVenue(VenueAdapter):
    name = "kalshi"

    def __init__(
        self,
        config: KalshiConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or KalshiConfig.from_env()
        self.name = f"kalshi:{self.config.environment}"
        self.client = client or httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "NOEMA/0.1"},
        )
        self.signer = (
            KalshiSigner(self.config.key_id, self.config.private_key_path)
            if self.config.key_id and self.config.private_key_path
            else None
        )

    @property
    def supports_live_execution(self) -> bool:
        return (
            self.config.environment == "production"
            and self.config.allow_live_orders
            and not self.config.master_halt
            and self.signer is not None
        )

    @property
    def supports_demo_execution(self) -> bool:
        return self.config.environment == "demo" and self.signer is not None

    async def close(self) -> None:
        await self.client.aclose()

    async def markets(self) -> AsyncIterator[MarketSnapshot]:
        cursor: str | None = None
        while True:
            markets, cursor = await self.market_page(cursor=cursor, limit=1000)
            for market in markets:
                yield market
            if not cursor:
                break

    async def market_page(
        self, *, cursor: str | None = None, limit: int = 100,
    ) -> tuple[list[MarketSnapshot], str | None]:
        """Fetch one official API page; callers can persist its opaque cursor."""
        if not 1 <= limit <= 1000:
            raise ValueError("Kalshi market page limit must be 1..1000")
        params: dict[str, Any] = {"status": "open", "limit": limit,
                                  "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        response = await self.client.get("/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Kalshi market page is malformed")
        raw_markets = payload.get("markets")
        next_cursor = payload.get("cursor")
        if (not isinstance(raw_markets, list)
                or any(not isinstance(m, dict) for m in raw_markets)
                or next_cursor is not None and not isinstance(next_cursor, str)):
            raise ValueError("Kalshi market page is malformed")
        snapshots = [self._market_snapshot(m, environment=self.config.environment)
                     for m in raw_markets]
        if any(snapshot is None for snapshot in snapshots):
            raise ValueError("Kalshi market page contains a market without a ticker")
        return [snapshot for snapshot in snapshots if snapshot is not None], next_cursor or None

    async def orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        if self.signer is None:
            raise RuntimeError("Kalshi orderbook access requires API credentials")
        params = {"depth": depth} if depth is not None else None
        endpoint = f"/markets/{ticker}/orderbook"
        sign_path = f"/trade-api/v2{endpoint}"
        headers = self.signer.headers("GET", sign_path)
        response = await self.client.get(endpoint, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    async def paper_book(self, ticker: str) -> tuple[dict[str, Any], str]:
        """Use full authenticated depth, or a fresh public top-of-book quote."""
        if self.signer is not None:
            return await self.orderbook(ticker), "authenticated_orderbook"
        if not ticker or not all(char.isalnum() or char == "-" for char in ticker):
            raise ValueError("invalid market ticker")
        response = await self.client.get(f"/markets/{quote(ticker, safe='')}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("market"), dict):
            raise TypeError("missing current market")
        market = payload["market"]
        if (market.get("ticker") != ticker or market.get("status") != "open"
                or market.get("mve_collection_ticker")):
            raise ValueError("market is not an open standalone contract")
        # The public endpoint exposes prices and quantities only at the best
        # level. A larger proposed fill must fail instead of assuming depth.
        try:
            bid, ask, bid_size, ask_size = (
                Decimal(str(market[key])) for key in
                ("yes_bid_dollars", "yes_ask_dollars", "yes_bid_size_fp", "yes_ask_size_fp")
            )
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ValueError("missing valid public best bid/ask and size") from exc
        if (not all(value.is_finite() for value in (bid, ask, bid_size, ask_size))
                or not 0 < bid <= ask < 1 or bid_size <= 0 or ask_size <= 0):
            raise ValueError("missing valid public best bid/ask and size")
        return {
            "orderbook_fp": {
                "yes_dollars": [[str(bid), str(bid_size)]],
                "no_dollars": [[str(1 - ask), str(ask_size)]],
            },
        }, "public_top_of_book"

    async def taker_fee_terms(self, ticker: str) -> FeeTerms:
        """Read the current series fee terms and event overrides, fail closed."""
        series, sep, _ = ticker.partition("-")
        event, event_sep, _ = ticker.rpartition("-")
        if not sep or not event_sep or not all(
            part and all(char.isalnum() or char == "-" for char in part)
            for part in (series, event)
        ):
            raise ValueError("invalid market ticker")
        series_response = await self.client.get(f"/series/{quote(series, safe='')}")
        series_response.raise_for_status()
        event_response = await self.client.get(f"/events/{quote(event, safe='')}")
        event_response.raise_for_status()
        series_payload = series_response.json()
        event_payload = event_response.json()
        if not isinstance(series_payload, dict) or not isinstance(event_payload, dict):
            raise TypeError("missing fee metadata")
        series_data = series_payload.get("series")
        event_data = event_payload.get("event")
        if (not isinstance(series_data, dict) or not isinstance(event_data, dict)
                or series_data.get("ticker") != series
                or event_data.get("event_ticker") != event):
            raise ValueError("fee metadata does not match the market")
        return FeeTerms.from_api(series_data, event_data)

    async def exchange_status(self) -> dict[str, Any]:
        response = await self.client.get("/exchange/status")
        response.raise_for_status()
        return response.json()

    async def event_market_tickers(self, event_ticker: str) -> set[str]:
        """Read the full current event membership before treating it as binary."""
        if not event_ticker or not all(c.isalnum() or c == "-" for c in event_ticker):
            raise ValueError("invalid event ticker")
        response = await self.client.get(
            f"/events/{quote(event_ticker, safe='')}",
            params={"with_nested_markets": "true"},
        )
        response.raise_for_status()
        payload = response.json()
        event = payload.get("event") or {}
        if event.get("event_ticker") != event_ticker:
            raise ValueError("event ticker mismatch")
        markets = event.get("markets") or payload.get("markets") or []
        tickers = {m.get("ticker") for m in markets}
        if not tickers or None in tickers:
            raise ValueError("event markets missing tickers")
        return tickers

    async def execute_demo(self, action: Action) -> str:
        if not self.supports_demo_execution:
            raise RuntimeError("Kalshi demo execution requires demo API credentials")
        return await self._submit_event_order(action)

    async def execute(self, action: Action) -> str:
        if self.config.master_halt:
            raise RuntimeError("NOEMA master halt is active")
        if not self.supports_live_execution:
            raise RuntimeError(
                "Kalshi production execution is disabled. "
                "Set production config, credentials, and NOEMA_ALLOW_LIVE_ORDERS=1."
            )
        return await self._submit_event_order(action)

    async def _submit_event_order(self, action: Action) -> str:
        if self.signer is None:
            raise RuntimeError("Kalshi API credentials are not configured")
        if action.max_price is None or action.stake_usd <= 0:
            raise ValueError("action has no executable price or stake")
        if action.decision not in {Decision.LIVE_BUY_YES, Decision.PAPER_BUY_YES}:
            raise NotImplementedError("initial Kalshi executor supports YES buys only")

        price = Decimal(str(action.max_price))
        if not Decimal(0) < price < Decimal(1):
            raise ValueError("Kalshi binary price must be between 0 and 1")

        count = Decimal(str(action.stake_usd)) / price
        path = "/trade-api/v2/portfolio/events/orders"
        body = {
            "ticker": action.market_id,
            "client_order_id": str(uuid.uuid4()),
            "side": "bid",
            "count": f"{count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": True,
            "reduce_only": False,
            "subaccount": 0,
            "exchange_index": 0,
        }
        headers = self.signer.headers("POST", path)
        response = await self.client.post(
            "/portfolio/events/orders",
            json=body,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["order_id"])

    @staticmethod
    def _market_snapshot(
        raw: dict[str, Any], *, environment: str = "demo"
    ) -> MarketSnapshot | None:
        ticker = raw.get("ticker")
        if not ticker:
            return None

        rules = "\n\n".join(
            part for part in [raw.get("rules_primary"), raw.get("rules_secondary")] if part
        )
        return MarketSnapshot(
            venue=f"kalshi:{environment}",
            market_id=str(ticker),
            title=str(raw.get("title") or ticker),
            yes_bid=_decimal(raw.get("yes_bid_dollars")),
            yes_ask=_decimal(raw.get("yes_ask_dollars")),
            no_bid=_decimal(raw.get("no_bid_dollars")),
            no_ask=_decimal(raw.get("no_ask_dollars")),
            liquidity_usd=_estimated_top_book_liquidity(raw),
            closes_at=_dt(raw.get("close_time") or raw.get("expiration_time")),
            resolution_rules=rules or None,
        )
