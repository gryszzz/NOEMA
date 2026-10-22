"""Authenticated Polymarket US trading adapter (api.polymarket.us).

The read-only market adapter never submits orders. This adapter uses the
official ``polymarket-us`` SDK against the US retail API. It does not speak to
the international CLOB.

Live entry follows the Kalshi gate: credentials must be configured,
``NOEMA_ALLOW_LIVE_ORDERS=1``, and ``NOEMA_MASTER_HALT`` must be off. The
retail create-order schema has no client-order field
(https://docs.polymarket.us/api-reference/orders/create-order), so the gateway's
stable id is stored locally and is not sent as an undocumented body field. A
retry returns the saved venue order id. An unknown dispatch is not submitted
again.

Signing, request headers, and secret values stay inside the SDK and the
credential boundary. This module logs only stable error codes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from noema.config import PolymarketUSTradingConfig
from noema.execution_gateway import (
    POLYMARKET_US_LIVE_VENUE,
    ExecutionGateway,
    ExecutionProposal,
    ReconciliationObservation,
    configured_live_venues,
)
from noema.models import Action, Decision, MarketSnapshot
from noema.venues.base import VenueAdapter
from noema.venues.errors import VenueRejected, VenueUnavailable
from noema.wallet_credentials import load_polymarket_us_credentials_in_api_boundary

from ..sqlite_util import connect as sqlite_connect

_log = logging.getLogger(__name__)

_PRICE_QUANTUM = Decimal("0.0001")
_QTY_QUANTUM = Decimal("0.0001")
_MIN_PRICE = Decimal("0.01")
_MAX_PRICE = Decimal("0.99")
_ACCOUNT_STATE_DEFAULT_SECONDS = 30.0
_CLIENT_ID_FIELDS = (
    "clientOrderId", "client_order_id", "clordId", "clOrdId", "clord_id",
)
_INTENTS = {
    "buy_yes": "ORDER_INTENT_BUY_LONG",
    "sell_yes": "ORDER_INTENT_SELL_LONG",
    "buy_no": "ORDER_INTENT_BUY_SHORT",
    "sell_no": "ORDER_INTENT_SELL_SHORT",
    "ORDER_INTENT_BUY_LONG": "ORDER_INTENT_BUY_LONG",
    "ORDER_INTENT_SELL_LONG": "ORDER_INTENT_SELL_LONG",
    "ORDER_INTENT_BUY_SHORT": "ORDER_INTENT_BUY_SHORT",
    "ORDER_INTENT_SELL_SHORT": "ORDER_INTENT_SELL_SHORT",
}
_TIF = {
    "gtc": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    "gtd": "TIME_IN_FORCE_GOOD_TILL_DATE",
    "ioc": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "fok": "TIME_IN_FORCE_FILL_OR_KILL",
    "day": "TIME_IN_FORCE_DAY",
    "TIME_IN_FORCE_GOOD_TILL_CANCEL": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    "TIME_IN_FORCE_GOOD_TILL_DATE": "TIME_IN_FORCE_GOOD_TILL_DATE",
    "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "TIME_IN_FORCE_FILL_OR_KILL": "TIME_IN_FORCE_FILL_OR_KILL",
    "TIME_IN_FORCE_DAY": "TIME_IN_FORCE_DAY",
}
_ORDER_STATUS = {
    "ORDER_STATE_PENDING_NEW": "submitted",
    "ORDER_STATE_NEW": "submitted",
    "ORDER_STATE_PENDING_CANCEL": "submitted",
    "ORDER_STATE_PENDING_REPLACE": "submitted",
    "ORDER_STATE_PENDING_RISK": "submitted",
    "ORDER_STATE_PARTIALLY_FILLED": "partial",
    "ORDER_STATE_FILLED": "filled",
    "ORDER_STATE_CANCELED": "cancelled",
    "ORDER_STATE_REPLACED": "cancelled",
    "ORDER_STATE_REJECTED": "rejected",
    "ORDER_STATE_EXPIRED": "expired",
}
_REJECTED_STATUS = {400, 401, 403, 404, 405, 413, 422}
_REJECTED_TYPES = {"BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError"}
_UNAVAILABLE_TYPES = {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, dict):
        return _decimal(value.get("value"))
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def _money_text(value: Any) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    return format(number, "f")


def _json_quantity(value: Decimal) -> int | float:
    text = format(value, "f")
    if "." not in text:
        return int(text)
    return float(text)


def _validate_client_order_id(value: str | None) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("client_order_id must be 1..64 characters")
    if not value.isascii() or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise ValueError("client_order_id must be visible ASCII")
    return value


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not slug or "/" in slug or any(char.isspace() for char in slug):
        raise ValueError("invalid Polymarket US market slug")
    return slug


def _map_sdk_error(exc: BaseException) -> VenueRejected | VenueUnavailable:
    """Collapse provider failures to stable codes. Never copy the provider message."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    if status == 409 or name in {"ConflictError", "APIConflictError"}:
        return VenueUnavailable("venue_conflict")
    if name in _REJECTED_TYPES or (status is not None and status in _REJECTED_STATUS):
        return VenueRejected(f"venue_rejected_{status if status is not None else name}")
    if name in _UNAVAILABLE_TYPES or status in {408, 429, 500, 502, 503, 504}:
        return VenueUnavailable("venue_unavailable")
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return VenueUnavailable("venue_unavailable")
    return VenueUnavailable("venue_unavailable")


def _fee_facts(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    nodes = [payload]
    for key in ("order", "preview", "estimatedOrder"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nodes.append(nested)
    facts: dict[str, str] = {}
    for node in nodes:
        for key in (
            "commissionsBasisPoints", "makerCommissionsBasisPoints", "takerCommissionsBasisPoints",
        ):
            parsed = _decimal(node.get(key))
            if parsed is not None and parsed >= 0:
                facts[key] = format(parsed, "f")
        for key in ("estimatedCommission", "commissionNotional", "commissionNotionalCollected"):
            parsed = _decimal(node.get(key))
            if parsed is not None and parsed >= 0:
                facts[key] = format(parsed, "f")
    return facts or None


def _book_touch(payload: Any, slug: str) -> tuple[Decimal, Decimal]:
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("marketSlug") != slug:
        raise VenueUnavailable("book_mismatch")
    bids = data.get("bids")
    offers = data.get("offers") if isinstance(data.get("offers"), list) else data.get("asks")
    if not isinstance(bids, list) or not isinstance(offers, list) or not bids or not offers:
        raise VenueUnavailable("book_depth_unverified")

    def levels(rows: list[Any]) -> list[tuple[Decimal, Decimal]]:
        parsed: list[tuple[Decimal, Decimal]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise VenueUnavailable("book_depth_unverified")
            price = _decimal(row.get("px") if "px" in row else row.get("price"))
            quantity = _decimal(row.get("qty") if "qty" in row else row.get("quantity"))
            if (price is None or quantity is None or quantity <= 0
                    or not Decimal(0) < price < Decimal(1)):
                raise VenueUnavailable("book_depth_unverified")
            parsed.append((price, quantity))
        return parsed

    bid_levels = levels(bids)
    ask_levels = levels(offers)
    best_bid = max(price for price, _quantity in bid_levels)
    best_ask = min(price for price, _quantity in ask_levels)
    if best_bid > best_ask:
        raise VenueUnavailable("book_crossed")
    return best_bid, best_ask


def _account_age_seconds() -> float:
    raw = os.getenv("NOEMA_ACCOUNT_STATE_MAX_AGE_SECONDS", str(_ACCOUNT_STATE_DEFAULT_SECONDS))
    try:
        age = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if age < 0 or math.isnan(age):
        return 0.0
    return age


@dataclass(frozen=True)
class PolymarketPrivateEvent:
    type: str
    payload: dict[str, Any]
    received_at: datetime


class _ClientOrderStore:
    """Local map from the gateway client id to the venue order id. No secrets."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS polymarket_us_client_orders (
                client_order_id TEXT PRIMARY KEY,
                venue_order_id TEXT,
                market_slug TEXT NOT NULL,
                intent TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        return conn

    def get(self, client_order_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM polymarket_us_client_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()

    def insert_pending(
        self, *, client_order_id: str, market_slug: str, intent: str,
        price: str, quantity: str,
    ) -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO polymarket_us_client_orders "
                    "(client_order_id,venue_order_id,market_slug,intent,price,quantity,state,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (client_order_id, None, market_slug, intent, price, quantity, "pending",
                     datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def mark(self, client_order_id: str, state: str, venue_order_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE polymarket_us_client_orders SET state=?, venue_order_id=COALESCE(?, venue_order_id) "
                "WHERE client_order_id=?",
                (state, venue_order_id, client_order_id),
            )


class PolymarketUSTradingVenue(VenueAdapter):
    """Polymarket US order adapter. Live entry is off unless the Kalshi-style gates pass."""

    name = POLYMARKET_US_LIVE_VENUE
    supports_authoritative_reconciliation = True

    def __init__(
        self,
        config: PolymarketUSTradingConfig | None = None,
        client: Any | None = None,
        *,
        idempotency_path: str | None = None,
    ) -> None:
        self.config = config or PolymarketUSTradingConfig.from_env()
        self._client = client
        self._orders = _ClientOrderStore(
            idempotency_path or os.getenv(
                "NOEMA_POLYMARKET_US_ORDER_DB", "data/polymarket_us_client_orders.sqlite3",
            )
        )
        self._account_verified_at: datetime | None = None
        self._economics: dict[str, Any] | None = None
        self._account_state_max_age_seconds = _account_age_seconds()

    @property
    def supports_live_execution(self) -> bool:
        return (
            self.config.credentials_configured
            and self.config.allow_live_orders
            and not self.config.master_halt
        )

    @property
    def execution_economics_verified(self) -> bool:
        return bool(self._economics and self._economics.get("market_slug"))

    @property
    def execution_account_state_current(self) -> bool:
        if self._account_verified_at is None:
            return False
        age = (datetime.now(UTC) - self._account_verified_at).total_seconds()
        return age <= self._account_state_max_age_seconds

    @property
    def client(self) -> Any:
        if self._client is None:
            key_id, secret_key = load_polymarket_us_credentials_in_api_boundary()
            from polymarket_us import PolymarketUS

            self._client = PolymarketUS(
                key_id=key_id, secret_key=secret_key, timeout=15.0, max_retries=0,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()

    async def markets(self) -> AsyncIterator[MarketSnapshot]:
        from noema.venues.polymarket_us import PolymarketUSVenue

        if self._client is not None:
            public = PolymarketUSVenue(client=self._client)
            async for market in public.markets():
                yield market
            return
        public = PolymarketUSVenue()
        try:
            async for market in public.markets():
                yield market
        finally:
            public.close()

    async def _call(self, method: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(method, *args)
        except (VenueRejected, VenueUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - map provider failures to typed venue errors
            mapped = _map_sdk_error(exc)
            _log.warning("polymarket_us_request_failed code=%s", mapped.code)
            raise mapped from None

    async def balances(self) -> dict[str, Any]:
        payload = await self._call(self.client.account.balances)
        if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
            raise VenueUnavailable("balances_malformed")
        rows: list[dict[str, str | None]] = []
        buying_power = None
        current_balance = None
        for item in payload["balances"]:
            if not isinstance(item, dict):
                raise VenueUnavailable("balances_malformed")
            currency = str(item.get("currency") or "")
            row_buying = _money_text(item.get("buyingPower"))
            row_current = _money_text(item.get("currentBalance"))
            rows.append({
                "currency": currency,
                "buying_power": row_buying,
                "current_balance": row_current,
            })
            if currency.upper() == "USD":
                buying_power = row_buying
                current_balance = row_current
        return {
            "balances": rows,
            "buying_power_usd": buying_power,
            "current_balance_usd": current_balance,
        }

    async def positions(self) -> list[dict[str, Any]]:
        payload = await self._call(self.client.portfolio.positions, {})
        if not isinstance(payload, dict):
            raise VenueUnavailable("positions_malformed")
        raw = payload.get("positions")
        if isinstance(raw, dict):
            records = list(raw.values())
        elif isinstance(raw, list):
            records = raw
        else:
            raise VenueUnavailable("positions_malformed")
        positions: list[dict[str, Any]] = []
        for row in records:
            if not isinstance(row, dict):
                raise VenueUnavailable("positions_malformed")
            market = row.get("marketMetadata") if isinstance(row.get("marketMetadata"), dict) else {}
            positions.append({
                "market_slug": market.get("slug") or row.get("marketSlug"),
                "outcome": market.get("outcome"),
                "net_position": row.get("netPositionDecimal", row.get("netPosition")),
                "available": row.get("qtyAvailableDecimal", row.get("qtyAvailable")),
                "cost_usd": _money_text(row.get("cost")),
                "realized_usd": _money_text(row.get("realized")),
                "cash_value_usd": _money_text(row.get("cashValue")),
            })
        return positions

    async def open_orders(self) -> list[dict[str, Any]]:
        payload = await self._call(self.client.orders.list)
        return self._order_rows(payload)

    async def fills(self, *, market_slug: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": 100, "types": ["ACTIVITY_TYPE_TRADE"], "sortOrder": "SORT_ORDER_DESCENDING",
        }
        if market_slug:
            params["marketSlug"] = _validate_slug(market_slug)
        payload = await self._call(self.client.portfolio.activities, params)
        if not isinstance(payload, dict) or not isinstance(payload.get("activities"), list):
            raise VenueUnavailable("fills_malformed")
        fills: list[dict[str, Any]] = []
        for activity in payload["activities"]:
            if not isinstance(activity, dict) or activity.get("type") != "ACTIVITY_TYPE_TRADE":
                continue
            trade = activity.get("trade") if isinstance(activity.get("trade"), dict) else {}
            fill_id = trade.get("id")
            if not fill_id:
                continue
            fills.append({
                "fill_id": str(fill_id),
                "market_slug": trade.get("marketSlug"),
                "quantity": trade.get("qtyDecimal", trade.get("qty")),
                "price_usd": _money_text(trade.get("price")),
                "fee_usd": _money_text(
                    trade.get("commissionNotionalCollected") or trade.get("fee")
                ),
                "order_id": trade.get("orderId") or trade.get("order_id"),
                "created_time": trade.get("createTime"),
                "state": trade.get("state"),
            })
        return fills

    async def order_status(self, order_id: str) -> dict[str, Any]:
        if not order_id:
            raise ValueError("order_id is required")
        payload = await self._call(self.client.orders.retrieve, order_id)
        order = payload.get("order") if isinstance(payload, dict) and isinstance(payload.get("order"), dict) else payload
        if not isinstance(order, dict) or not order.get("id"):
            raise VenueUnavailable("order_status_malformed")
        if str(order.get("id")) != str(order_id):
            raise VenueUnavailable("order_status_mismatch")
        return order

    async def prepare_live_execution(self, market_slug: str) -> dict[str, Any]:
        """Read buying power, positions, open orders, book depth, and a fee preview.

        The gateway refuses live entry until both verification flags are true.
        Preview uses the public order-preview endpoint and does not insert an order.
        """
        slug = _validate_slug(market_slug)
        balances = await self.balances()
        if balances.get("buying_power_usd") is None:
            raise VenueUnavailable("buying_power_unavailable")
        await self.positions()
        await self.open_orders()
        self._account_verified_at = datetime.now(UTC)
        book = await self._call(self.client.markets.book, slug)
        best_bid, best_ask = _book_touch(book, slug)
        if not _MIN_PRICE <= best_ask <= _MAX_PRICE:
            raise VenueUnavailable("book_price_out_of_bounds")
        preview_body = {
            "marketSlug": slug,
            "intent": "ORDER_INTENT_BUY_LONG",
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{best_ask:.4f}", "currency": "USD"},
            "quantity": 1,
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "participateDontInitiate": False,
        }
        preview = await self._call(self.client.orders.preview, preview_body)
        fees = _fee_facts(preview)
        if fees is None:
            raise VenueRejected("fee_schedule_unverified")
        self._economics = {
            "market_slug": slug,
            "best_bid": format(best_bid, "f"),
            "best_ask": format(best_ask, "f"),
            "fees": fees,
            "verified_at": datetime.now(UTC).isoformat(),
        }
        return {
            "market_slug": slug,
            "buying_power_usd": balances["buying_power_usd"],
            "best_bid": self._economics["best_bid"],
            "best_ask": self._economics["best_ask"],
            "fees": fees,
        }

    async def place_limit(
        self, market_slug: str, intent: str, price: Decimal | str | float,
        quantity: Decimal | str | float, tif: str, client_order_id: str,
        *, manual: bool = False,
    ) -> str:
        slug = _validate_slug(market_slug)
        venue_intent = _INTENTS.get(intent)
        venue_tif = _TIF.get(tif)
        if venue_intent is None or venue_tif is None:
            raise ValueError("unsupported Polymarket US intent or time in force")
        limit = self._limit_price(price)
        size = self._quantity(quantity)
        client_id = _validate_client_order_id(client_order_id)
        existing = self._resolve_existing(client_id)
        if existing is not None:
            return existing
        self._require_live_entry(slug)
        echoed = await self._echoed_order_id(client_id)
        if echoed is not None:
            self._orders.mark(client_id, "accepted", echoed)
            if self._orders.get(client_id) is None:
                self._orders.insert_pending(
                    client_order_id=client_id, market_slug=slug, intent=venue_intent,
                    price=f"{limit:.4f}", quantity=format(size, "f"),
                )
                self._orders.mark(client_id, "accepted", echoed)
            return echoed
        inserted = self._orders.insert_pending(
            client_order_id=client_id, market_slug=slug, intent=venue_intent,
            price=f"{limit:.4f}", quantity=format(size, "f"),
        )
        if not inserted:
            return self._require_saved_order(client_id)
        body = {
            "marketSlug": slug,
            "intent": venue_intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{limit:.4f}", "currency": "USD"},
            "quantity": _json_quantity(size),
            "tif": venue_tif,
            "manualOrderIndicator": (
                "MANUAL_ORDER_INDICATOR_MANUAL" if manual else "MANUAL_ORDER_INDICATOR_AUTOMATIC"
            ),
            "participateDontInitiate": False,
        }
        try:
            payload = await self._call(self.client.orders.create, body)
        except VenueRejected:
            self._orders.mark(client_id, "rejected", None)
            raise
        order_id = _created_order_id(payload)
        if order_id is None:
            raise VenueUnavailable("create_order_missing_id")
        self._orders.mark(client_id, "accepted", order_id)
        return order_id

    async def execute(self, action: Action, *, client_order_id: str | None = None) -> str:
        if client_order_id is None:
            raise ValueError("stable client_order_id is required")
        if action.decision is not Decision.LIVE_BUY_YES:
            raise NotImplementedError("Polymarket US live executor supports LIVE_BUY_YES only")
        if action.venue != self.name:
            raise ValueError("action venue does not match polymarket_us")
        if action.max_price is None or action.stake_usd <= 0:
            raise ValueError("action has no executable price or stake")
        price = self._limit_price(action.max_price)
        stake = Decimal(str(action.stake_usd))
        if not stake.is_finite() or stake <= 0:
            raise ValueError("action stake is invalid")
        quantity = (stake / price).quantize(_QTY_QUANTUM, rounding=ROUND_DOWN)
        if quantity <= 0 or quantity * price > stake:
            raise ValueError("action stake is below the minimum executable contract count")
        return await self.place_limit(
            action.market_id, "buy_yes", price, quantity, "ioc", client_order_id,
        )

    async def cancel(self, order_id: str, *, market_slug: str | None = None) -> dict[str, str]:
        if not order_id:
            raise ValueError("order_id is required")
        if self._client is None and not self.config.credentials_configured:
            raise VenueRejected("credentials_unavailable")
        slug = _validate_slug(market_slug) if market_slug else self._slug_for_order(order_id)
        if slug is None:
            for order in await self.open_orders():
                if str(order.get("order_id")) == str(order_id) and order.get("market_slug"):
                    slug = str(order["market_slug"])
                    break
        if slug is None:
            raise VenueRejected("market_slug_required")
        await self._call(self.client.orders.cancel, order_id, {"marketSlug": slug})
        return {"order_id": str(order_id), "market_slug": slug}

    async def reconcile_execution(self, request: dict[str, Any]) -> ReconciliationObservation | None:
        proposal_id = str(request.get("proposal_id") or "")
        venue = str(request.get("venue") or "")
        if not proposal_id or venue != self.name:
            raise ValueError("request identity does not match polymarket_us")
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, "noema:" + proposal_id))
        try:
            stored = json.loads(str(request.get("request_json") or "{}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("stored gateway request is malformed") from exc
        client_order_id = stored.get("client_order_id", expected)
        if client_order_id != expected:
            raise ValueError("stored client order ID does not match proposal identity")
        _validate_client_order_id(str(client_order_id))
        row = self._orders.get(str(client_order_id))
        order_id = row["venue_order_id"] if row is not None else None
        if not order_id:
            echoed = await self._echoed_order_id(str(client_order_id))
            if echoed is None:
                return None
            order_id = echoed
        order = await self.order_status(str(order_id))
        instrument = str(request.get("instrument") or "")
        if not instrument or str(order.get("marketSlug") or "") != instrument:
            raise ValueError("Polymarket US order instrument does not match the gateway request")
        status = _ORDER_STATUS.get(str(order.get("state") or ""))
        if status is None:
            raise ValueError("Polymarket US order status is unrecognized")
        filled_quantity = _decimal(order.get("cumQuantity"))
        average_price = _decimal(order.get("avgPx"))
        fee = _decimal(order.get("commissionNotionalTotalCollected"))
        if filled_quantity is None or filled_quantity < 0:
            raise ValueError("Polymarket US filled quantity is invalid")
        if fee is None:
            fee = Decimal(0)
        if fee < 0:
            raise ValueError("Polymarket US commission is invalid")
        if filled_quantity == 0:
            filled_exposure = fee
        elif average_price is None or not Decimal(0) < average_price < Decimal(1):
            return None
        else:
            filled_exposure = filled_quantity * average_price + fee
        if status == "submitted" and filled_quantity > 0:
            status = "partial"
        requested = _decimal(request.get("notional_usd")) or Decimal(0)
        if status in {"filled", "rejected", "cancelled", "expired", "failed"}:
            remaining = Decimal(0)
        else:
            remaining = max(Decimal(0), requested - filled_exposure)
        fill_ids: list[str] = []
        executions = order.get("executions") if isinstance(order.get("executions"), list) else []
        for execution in executions:
            if isinstance(execution, dict) and execution.get("tradeId"):
                fill_ids.append(str(execution["tradeId"]))
        return ReconciliationObservation(
            provider=self.name, proposal_id=proposal_id, provider_reference=str(order_id),
            status=status, observed_at=datetime.now(UTC),
            source="polymarket_us_authenticated_order_api",
            filled_quantity=filled_quantity, fee_amount=fee, fee_currency="USD",
            external_fill_ids=tuple(sorted(set(fill_ids))),
            filled_exposure_usd=filled_exposure, remaining_reserved_usd=remaining,
        )

    async def stream_private(self, *, reconnect: bool = True) -> AsyncIterator[PolymarketPrivateEvent]:
        """Yield private order, fill, position, and balance events with reconnect backoff."""
        if self._client is None and not self.config.credentials_configured:
            raise RuntimeError("Polymarket US private websocket requires API credentials")
        backoff = 1.0
        while True:
            websocket = None
            try:
                websocket = self.client.ws.private()
                queue: asyncio.Queue[PolymarketPrivateEvent | BaseException] = asyncio.Queue()
                disconnected = asyncio.Event()

                def emit(
                    name: str,
                    payload: Any,
                    event_queue: asyncio.Queue[PolymarketPrivateEvent | BaseException] = queue,
                    event_disconnected: asyncio.Event = disconnected,
                ) -> None:
                    if name == "error":
                        event_queue.put_nowait(ConnectionError("private websocket error"))
                        event_disconnected.set()
                        return
                    if name in {"close", "open"} or not isinstance(payload, dict):
                        if name == "close":
                            event_disconnected.set()
                        return
                    event_queue.put_nowait(PolymarketPrivateEvent(
                        type=_stream_event_type(name, payload), payload=payload,
                        received_at=datetime.now(UTC),
                    ))

                websocket.on("open", lambda: emit("open", None))
                websocket.on("close", lambda: emit("close", None))
                websocket.on("error", lambda error=None: emit("error", error))
                for name in (
                    "order_snapshot", "order_update", "position_snapshot", "position_update",
                    "account_balance_snapshot", "account_balance_update",
                ):
                    websocket.on(name, lambda data, event_name=name: emit(event_name, data))
                await websocket.connect()
                await _subscribe_private(websocket)
                backoff = 1.0
                while True:
                    if disconnected.is_set() and queue.empty():
                        raise ConnectionError("private websocket disconnected")
                    item = await _next_stream_item(queue, disconnected)
                    if isinstance(item, BaseException):
                        raise item
                    yield item
            except asyncio.CancelledError:
                raise
            except Exception:
                if not reconnect:
                    raise
                _log.warning("polymarket_us_private_stream_reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if websocket is not None and hasattr(websocket, "close"):
                    try:
                        await websocket.close()
                    except Exception:  # noqa: BLE001 - close failures must not hide the stream error
                        _log.debug("polymarket_us_private_stream_close_failed")

    def _require_live_entry(self, market_slug: str) -> None:
        if os.getenv("NOEMA_MASTER_HALT", "0") == "1" or self.config.master_halt:
            raise VenueRejected("master_halt")
        if os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") != "1" or not self.config.allow_live_orders:
            raise VenueRejected("live_orders_disabled")
        if not self.config.credentials_configured:
            raise VenueRejected("credentials_unavailable")
        if not self.execution_account_state_current:
            raise VenueRejected("account_state_not_current")
        if self._economics is None or self._economics.get("market_slug") != market_slug:
            raise VenueRejected("execution_economics_not_verified")

    def _resolve_existing(self, client_order_id: str) -> str | None:
        row = self._orders.get(client_order_id)
        if row is None:
            return None
        if row["state"] == "accepted" and row["venue_order_id"]:
            return str(row["venue_order_id"])
        if row["state"] == "pending":
            raise VenueUnavailable("client_order_outcome_unknown")
        if row["state"] == "rejected":
            raise VenueRejected("client_order_previously_rejected")
        raise VenueUnavailable("client_order_outcome_unknown")

    def _require_saved_order(self, client_order_id: str) -> str:
        resolved = self._resolve_existing(client_order_id)
        if resolved is None:
            raise VenueUnavailable("client_order_outcome_unknown")
        return resolved

    async def _echoed_order_id(self, client_order_id: str) -> str | None:
        orders = await self.open_orders()
        matches = [
            str(order["order_id"]) for order in orders
            if order.get("order_id") and _order_client_id(order) == client_order_id
        ]
        if len(matches) > 1:
            raise VenueUnavailable("duplicate_client_order_identity")
        return matches[0] if matches else None

    def _slug_for_order(self, order_id: str) -> str | None:
        with self._orders._connect() as conn:
            row = conn.execute(
                "SELECT market_slug FROM polymarket_us_client_orders WHERE venue_order_id=?",
                (order_id,),
            ).fetchone()
        if row is None or not row["market_slug"]:
            return None
        return str(row["market_slug"])

    def _order_rows(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
            raise VenueUnavailable("orders_malformed")
        rows: list[dict[str, Any]] = []
        for order in payload["orders"]:
            if not isinstance(order, dict):
                raise VenueUnavailable("orders_malformed")
            rows.append({
                "order_id": order.get("id"),
                "market_slug": order.get("marketSlug"),
                "state": order.get("state") or order.get("status"),
                "intent": order.get("intent"),
                "side": order.get("side"),
                "price": _money_text(order.get("price")),
                "quantity": order.get("quantity"),
                "cum_quantity": order.get("cumQuantity"),
                "leaves_quantity": order.get("leavesQuantity"),
                "client_order_id": _order_client_id(order),
            })
        return rows

    @staticmethod
    def _limit_price(value: Decimal | str | float) -> Decimal:
        price = _decimal(value)
        if price is None:
            raise ValueError("Polymarket US limit price is invalid")
        quantized = price.quantize(_PRICE_QUANTUM, rounding=ROUND_DOWN)
        if quantized != price or not _MIN_PRICE <= price <= _MAX_PRICE:
            raise ValueError("Polymarket US limit price must be between 0.01 and 0.99 on a 0.0001 tick")
        return price

    @staticmethod
    def _quantity(value: Decimal | str | float) -> Decimal:
        quantity = _decimal(value)
        if quantity is None:
            raise ValueError("Polymarket US quantity is invalid")
        quantized = quantity.quantize(_QTY_QUANTUM, rounding=ROUND_DOWN)
        if quantized != quantity or quantity <= 0:
            raise ValueError("Polymarket US quantity must be a positive 0.0001 increment")
        return quantity


def _created_order_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("id"):
        return str(payload["id"])
    order = payload.get("order")
    if isinstance(order, dict) and order.get("id"):
        return str(order["id"])
    return None


def _order_client_id(order: dict[str, Any]) -> str | None:
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else order
    for key in _CLIENT_ID_FIELDS:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _stream_event_type(name: str, payload: dict[str, Any]) -> str:
    if name.startswith("account_balance"):
        return "balance"
    if name.startswith("position"):
        return "position"
    update = payload.get("orderSubscriptionUpdate")
    execution = update.get("execution") if isinstance(update, dict) else None
    if isinstance(execution, dict) and (
        execution.get("tradeId") or str(execution.get("type") or "").endswith("FILL")
    ):
        return "fill"
    return "order"


async def _subscribe_private(websocket: Any) -> None:
    if hasattr(websocket, "subscribe_account_balance"):
        await websocket.subscribe_account_balance("noema-pm-balance")
        await websocket.subscribe_positions("noema-pm-positions")
        await websocket.subscribe_orders("noema-pm-orders")
        if hasattr(websocket, "subscribe"):
            await websocket.subscribe("noema-pm-order-snapshot", "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT")
        return
    await websocket.subscribe("noema-pm-orders", "SUBSCRIPTION_TYPE_ORDER")
    await websocket.subscribe("noema-pm-positions", "SUBSCRIPTION_TYPE_POSITION")
    await websocket.subscribe("noema-pm-balance", "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE")


async def _next_stream_item(
    queue: asyncio.Queue[PolymarketPrivateEvent | BaseException],
    disconnected: asyncio.Event,
) -> PolymarketPrivateEvent | BaseException:
    if not queue.empty():
        return queue.get_nowait()
    item_task = asyncio.create_task(queue.get())
    stop_task = asyncio.create_task(disconnected.wait())
    try:
        done, _pending = await asyncio.wait(
            {item_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        item_task.cancel()
        stop_task.cancel()
        raise
    if item_task in done:
        stop_task.cancel()
        return item_task.result()
    item_task.cancel()
    if not queue.empty():
        return queue.get_nowait()
    return ConnectionError("private websocket disconnected")


def manual_order_blockers(notional: Decimal) -> list[str]:
    """Owner CLI gates. These are necessary and do not replace the agent risk checks."""
    reasons: list[str] = []
    if os.getenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "0") != "1":
        reasons.append("execution gateway is disabled")
    if os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") != "1":
        reasons.append("owner live-order setting is disabled")
    if os.getenv("NOEMA_MASTER_HALT", "0") == "1":
        reasons.append("owner master halt is active")
    if POLYMARKET_US_LIVE_VENUE not in configured_live_venues():
        reasons.append("polymarket_us is not explicitly allowlisted")
    cap = ExecutionGateway._decimal(os.getenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "0"))
    if cap is None or cap <= 0:
        reasons.append("positive owner daily execution limit is missing")
    elif notional > cap:
        reasons.append("order exceeds the owner daily execution limit")
    return reasons


def _manual_proposal(
    *, proposal_id: str, slug: str, side: str, price: Decimal, quantity: Decimal,
) -> ExecutionProposal:
    now = datetime.now(UTC)
    notional = price * quantity
    return ExecutionProposal(
        proposal_id=proposal_id, mission_id="owner-manual", venue=POLYMARKET_US_LIVE_VENUE,
        instrument=slug, side=side, notional_usd=notional, max_loss_usd=notional,
        limit_price=price, expected_edge=Decimal(0), evidence_refs=("owner_manual_order",),
        created_at=now, expires_at=now + timedelta(minutes=5),
    )


async def run_pm_account(venue: PolymarketUSTradingVenue | None = None) -> dict[str, Any]:
    account = venue or PolymarketUSTradingVenue()
    try:
        return {
            "venue": POLYMARKET_US_LIVE_VENUE,
            "balances": await account.balances(),
            "positions": await account.positions(),
            "open_orders": await account.open_orders(),
        }
    finally:
        if venue is None:
            account.close()


async def run_pm_order(
    *, slug: str, side: str, price: Decimal, quantity: Decimal, tif: str = "gtc",
    live: bool = False, client_order_id: str | None = None, db_path: str = "data/noema.db",
    venue: PolymarketUSTradingVenue | None = None,
) -> dict[str, Any]:
    """Dry-run records a proposal. ``--live`` places a limit and then cancels it."""
    if side not in {"buy_yes", "sell_yes", "buy_no", "sell_no"}:
        raise ValueError("side must be buy_yes, sell_yes, buy_no, or sell_no")
    limit = PolymarketUSTradingVenue._limit_price(price)
    size = PolymarketUSTradingVenue._quantity(quantity)
    notional = limit * size
    proposal_id = client_order_id or str(uuid.uuid4())
    gateway = ExecutionGateway(db_path)
    proposal = _manual_proposal(
        proposal_id=proposal_id, slug=_validate_slug(slug), side=side, price=limit, quantity=size,
    )
    if not live:
        gateway.record_proposal(proposal)
        return {
            "status": "proposed", "live": False, "proposal_id": proposal_id,
            "market_slug": slug, "side": side, "price": f"{limit:.4f}",
            "quantity": format(size, "f"), "notional_usd": format(notional, "f"),
        }
    reasons = manual_order_blockers(notional)
    if reasons:
        return {"status": "rejected", "live": False, "reasons": reasons, "proposal_id": proposal_id}
    account = venue or PolymarketUSTradingVenue()
    owns_client = venue is None
    try:
        if not account.supports_live_execution:
            return {
                "status": "rejected", "live": False,
                "reasons": ["venue adapter does not support live execution"],
                "proposal_id": proposal_id,
            }
        stable_id = _validate_client_order_id(client_order_id or proposal_id)
        await account.prepare_live_execution(slug)
        order_id = await account.place_limit(
            slug, side, limit, size, tif, stable_id, manual=True,
        )
        payload = {
            "proposal_id": proposal_id, "mission_id": "owner-manual", "instrument": slug,
            "side": side, "client_order_id": stable_id, "notional_usd": format(notional, "f"),
        }
        gateway._record(
            proposal_id, "prediction", "execution", POLYMARKET_US_LIVE_VENUE, "submitted",
            notional, payload, [],
        )
        gateway._finish(proposal_id, "submitted", order_id, {})
        try:
            await account.cancel(order_id, market_slug=slug)
        except (VenueRejected, VenueUnavailable) as exc:
            gateway._finish(proposal_id, "submitted", order_id, {"cancel_error": exc.code})
            return {
                "status": "submitted", "live": True, "order_id": order_id,
                "cancel_error": exc.code, "proposal_id": proposal_id,
            }
        gateway._finish(proposal_id, "cancelled", order_id, {"reason": "owner_cli_cancel"})
        return {
            "status": "cancelled", "live": True, "order_id": order_id,
            "proposal_id": proposal_id, "market_slug": slug,
        }
    finally:
        if owns_client:
            account.close()
