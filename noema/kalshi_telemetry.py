from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from .config import KalshiConfig
from .trade_telemetry import (
    ObservedFill,
    ObservedOrder,
    ObservedPosition,
    QueueObservation,
    decimal_value,
)
from .venues.kalshi import KalshiSigner


def _dt(value: object | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _price(raw: dict[str, Any], name: str) -> Decimal | None:
    value = raw.get(name)
    return None if value is None else decimal_value(value)


_SECRETISH = re.compile(r"(?i)(key|token|secret|signature|authorization|password)([\"']?\s*[:=]\s*)[^\s,&}]+")


def _safe_body(response: httpx.Response, limit: int = 200) -> str:
    try:
        text = response.text[:limit]
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""
    return _SECRETISH.sub(r"\1\2[redacted]", " ".join(text.split()))


def _backoff(attempt: int, retry_after: str | None = None, base: float = 0.5, cap: float = 15.0) -> float:
    if retry_after:
        try:
            return min(cap, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(cap, base * (2 ** attempt)) * random.uniform(0.5, 1.5)


def _log_http_error(path: str, status: int | None, attempt: int, detail: str) -> None:
    # Path only: no query string, headers, key id or signature ever reach the log.
    print(json.dumps({"event": "kalshi_http_error", "path": path, "status": status,
                      "attempt": attempt, "detail": detail}), file=sys.stderr, flush=True)


class KalshiTelemetry:
    """Read-only authenticated order/fill/position telemetry."""

    def __init__(
        self,
        config: KalshiConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or KalshiConfig.from_env()
        if not self.config.key_id or not (
            self.config.private_key_path or self.config.private_key_pem
            or self.config.private_key_pem_b64
        ):
            raise RuntimeError("Kalshi telemetry requires API credentials")
        self.signer = KalshiSigner(
            self.config.key_id, self.config.private_key_path,
            private_key_pem=self.config.private_key_pem,
            private_key_pem_b64=self.config.private_key_pem_b64,
        )
        self.client = client or httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "NOEMA/0.1"},
        )
        self.coverage: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        retries: int = 4,
    ) -> dict[str, Any]:
        """Signed GET with jittered exponential backoff.

        Retries 429/5xx, timeouts and transport errors; a 403 is retried once with a fresh
        signature (a stale timestamp or a transient edge rejection looks identical to a bad key).
        Each failed attempt logs path, status and a redacted body excerpt, never headers.
        """
        sign_path = f"/trade-api/v2{endpoint}"
        forbidden_retried = False
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                response = await self.client.get(
                    endpoint,
                    params=params,
                    headers=self.signer.headers("GET", sign_path),  # re-signed every attempt
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if isinstance(exc, httpx.LocalProtocolError):
                    raise
                _log_http_error(sign_path, None, attempt, type(exc).__name__)
                if last:
                    raise
                await asyncio.sleep(_backoff(attempt))
                continue
            code = response.status_code
            if code < 400:
                return response.json()
            _log_http_error(sign_path, code, attempt, _safe_body(response))
            retryable = code == 429 or code >= 500 or (code == 403 and not forbidden_retried)
            if code == 403:
                forbidden_retried = True
            if last or not retryable:
                response.raise_for_status()
            await asyncio.sleep(_backoff(attempt, response.headers.get("retry-after")))
        raise RuntimeError("unreachable")

    async def orders(self) -> list[ObservedOrder]:
        rows = await self._get_all_pages("/portfolio/orders", "orders")
        return [parse_order(raw) for raw in rows]

    async def fills(self, *, order_id: str | None = None) -> list[ObservedFill]:
        params: dict[str, Any] = {"subaccount": 0}
        if order_id:
            params["order_id"] = order_id
        rows = await self._get_all_pages("/portfolio/fills", "fills", params=params)
        return [parse_fill(raw) for raw in rows]

    async def positions(self) -> list[ObservedPosition]:
        rows = await self._get_all_pages("/portfolio/positions", "market_positions")
        return [parse_position(raw) for raw in rows]

    async def settlements(self) -> list[dict[str, Any]]:
        return await self._get_all_pages("/portfolio/settlements", "settlements", limit=200)

    async def _get_all_pages(
        self, endpoint: str, collection: str, *, params: dict[str, Any] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_records: set[str] = set()
        pages = 0
        while pages < 10000:
            query = {"subaccount": 0, **(params or {}), "limit": limit}
            if cursor is not None:
                if cursor in seen_cursors:
                    raise ValueError(f"{endpoint} returned a repeated cursor")
                seen_cursors.add(cursor)
                query["cursor"] = cursor
            payload = await self._get(endpoint, params=query)
            page = payload.get(collection)
            if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
                raise ValueError(f"{endpoint} omitted its {collection} array")
            for row in page:
                identity = (row.get("fill_id") if collection == "fills" else
                            row.get("order_id") if collection == "orders" else
                            row.get("ticker") if collection == "market_positions" else
                            row.get("settlement_id") or (
                                f"{row.get('ticker')}:{row.get('settled_time')}"
                                if row.get("ticker") and row.get("settled_time") else None
                            ))
                if identity is not None:
                    token = str(identity)
                    if token in seen_records:
                        continue
                    seen_records.add(token)
                rows.append(row)
            pages += 1
            next_cursor = payload.get("cursor")
            if not next_cursor:
                self.coverage[collection] = {"complete": True, "pages": pages,
                                             "records": len(rows)}
                return rows
            if not isinstance(next_cursor, str):
                raise TypeError(f"{endpoint} returned an invalid cursor")
            cursor = next_cursor
        self.coverage[collection] = {"complete": False, "pages": pages,
                                     "records": len(rows), "reason": "page_limit_reached"}
        raise ValueError(f"{endpoint} exceeded the pagination safety limit")

    async def queue_position(self, order_id: str) -> QueueObservation:
        payload = await self._get(f"/portfolio/orders/{order_id}/queue_position")
        return QueueObservation(
            order_id=order_id,
            contracts_ahead=decimal_value(payload["queue_position_fp"]),
        )

    async def series_fee_changes(self) -> list[dict[str, Any]]:
        payload = await self._get(
            "/series/fee_changes",
            params={"show_historical": True},
        )
        return list(payload.get("series_fee_change_arr", []))

    async def event_fee_changes(self) -> list[dict[str, Any]]:
        payload = await self._get(
            "/events/fee_changes",
            params={"show_historical": True},
        )
        return list(payload.get("event_fee_change_arr", []))


def parse_order(raw: dict[str, Any]) -> ObservedOrder:
    return ObservedOrder(
        order_id=str(raw["order_id"]),
        ticker=str(raw.get("ticker") or raw.get("market_ticker") or ""),
        status=str(raw.get("status") or ""),
        initial_count=decimal_value(raw.get("initial_count_fp")),
        fill_count=decimal_value(raw.get("fill_count_fp")),
        remaining_count=decimal_value(raw.get("remaining_count_fp")),
        yes_price=_price(raw, "yes_price_dollars"),
        no_price=_price(raw, "no_price_dollars"),
        taker_fill_cost=decimal_value(raw.get("taker_fill_cost_dollars")),
        maker_fill_cost=decimal_value(raw.get("maker_fill_cost_dollars")),
        taker_fees=decimal_value(raw.get("taker_fees_dollars")),
        maker_fees=decimal_value(raw.get("maker_fees_dollars")),
        order_group_id=raw.get("order_group_id"),
        created_at=_dt(raw.get("created_time")),
        updated_at=_dt(raw.get("last_update_time")),
    )


def parse_fill(raw: dict[str, Any]) -> ObservedFill:
    return ObservedFill(
        fill_id=str(raw["fill_id"]),
        order_id=str(raw["order_id"]),
        ticker=str(raw.get("ticker") or raw.get("market_ticker") or ""),
        outcome_side=raw.get("outcome_side") or raw.get("side"),
        count=decimal_value(raw.get("count_fp")),
        yes_price=_price(raw, "yes_price_dollars"),
        no_price=_price(raw, "no_price_dollars"),
        is_taker=bool(raw.get("is_taker")),
        fee_cost=decimal_value(raw.get("fee_cost")),
        created_at=_dt(raw.get("created_time")),
    )


def parse_position(raw: dict[str, Any]) -> ObservedPosition:
    return ObservedPosition(
        ticker=str(raw["ticker"]),
        position=decimal_value(raw.get("position_fp")),
        total_traded=decimal_value(raw.get("total_traded_dollars")),
        exposure=decimal_value(raw.get("market_exposure_dollars")),
        realized_pnl=decimal_value(raw.get("realized_pnl_dollars")),
        fees_paid=decimal_value(raw.get("fees_paid_dollars")),
        updated_at=_dt(raw.get("last_updated_ts")),
    )
