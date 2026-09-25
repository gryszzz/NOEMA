from __future__ import annotations

import base64
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noema.config import KalshiConfig
from noema.models import Action, Decision, MarketSnapshot
from noema.venues.base import VenueAdapter


def _decimal(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(Decimal(str(value)))


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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
            params: dict[str, Any] = {
                "status": "open",
                "limit": 1000,
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor

            response = await self.client.get("/markets", params=params)
            response.raise_for_status()
            payload = response.json()

            for raw in payload.get("markets", []):
                snapshot = self._market_snapshot(raw)
                if snapshot is not None:
                    yield snapshot

            cursor = payload.get("cursor") or None
            if not cursor:
                break

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

    async def exchange_status(self) -> dict[str, Any]:
        response = await self.client.get("/exchange/status")
        response.raise_for_status()
        return response.json()

    async def execute_demo(self, action: Action) -> str:
        if not self.supports_demo_execution:
            raise RuntimeError("Kalshi demo execution requires demo API credentials")
        return await self._submit_event_order(action)

    async def execute(self, action: Action) -> str:
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
        if not Decimal("0") < price < Decimal("1"):
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
    def _market_snapshot(raw: dict[str, Any]) -> MarketSnapshot | None:
        ticker = raw.get("ticker")
        if not ticker:
            return None

        rules = "

".join(
            part for part in [raw.get("rules_primary"), raw.get("rules_secondary")] if part
        )
        return MarketSnapshot(
            venue="kalshi",
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
