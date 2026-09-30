from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import KalshiConfig
from .venues.kalshi import KalshiSigner


@dataclass(frozen=True)
class AccountSnapshot:
    balance: dict[str, Any]
    positions: dict[str, Any]
    orders: dict[str, Any]
    fills: dict[str, Any]
    settlements: dict[str, Any]
    deposits: dict[str, Any]
    withdrawals: dict[str, Any]
    transfers: dict[str, Any]
    limits: dict[str, Any]
    user_data_as_of: datetime | None


class KalshiAccount:
    """Authenticated account mirror for the primary Kalshi account."""

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
            raise RuntimeError("Kalshi account access requires API credentials")
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

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        retries: int = 4,
    ) -> dict[str, Any]:
        sign_path = f"/trade-api/v2{endpoint}"
        delay = 0.25
        for attempt in range(retries + 1):
            headers = self.signer.headers("GET", sign_path)
            response = await self.client.get(endpoint, params=params, headers=headers)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            if attempt == retries:
                response.raise_for_status()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 4.0)
        raise RuntimeError("unreachable")

    async def balance(self) -> dict[str, Any]:
        return await self._get("/portfolio/balance", params={"subaccount": 0})

    async def positions(self) -> dict[str, Any]:
        return await self._get_all_pages(
            "/portfolio/positions", "market_positions",
            params={"subaccount": 0, "count_filter": "position,total_traded"},
            additional_collections=("event_positions",), limit=1000,
        )

    async def orders(self) -> dict[str, Any]:
        return await self._get_all_pages(
            "/portfolio/orders", "orders", params={"subaccount": 0}, limit=1000,
        )

    async def fills(self, *, min_ts: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"subaccount": 0}
        if min_ts is not None:
            params["min_ts"] = min_ts
        return await self._get_all_pages(
            "/portfolio/fills", "fills", params=params, limit=1000,
        )

    async def historical_fills(self) -> dict[str, Any]:
        return await self._get_all_pages(
            "/historical/fills", "fills", params={"subaccount": 0}, limit=1000,
        )

    async def settlements(self, *, min_ts: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"subaccount": 0}
        if min_ts is not None:
            params["min_ts"] = min_ts
        return await self._get_all_pages(
            "/portfolio/settlements", "settlements", params=params, limit=1000,
        )

    async def deposits(self) -> dict[str, Any]:
        # The official history endpoint exposes cursors but no time filter or
        # documented ordering. Scan its complete (usually small) activity set.
        return await self._get_all_pages(
            "/portfolio/deposits", "deposits", params={}, limit=500,
        )

    async def withdrawals(self) -> dict[str, Any]:
        return await self._get_all_pages(
            "/portfolio/withdrawals", "withdrawals", params={}, limit=500,
        )

    async def transfers(self) -> dict[str, Any]:
        intra, subaccount = await asyncio.gather(
            self._get_all_pages("/portfolio/intra_exchange_instance_transfers", "transfers",
                                params={}, limit=500),
            self._get_all_pages("/portfolio/subaccounts/transfers", "transfers",
                                params={}, limit=1000),
        )
        rows: dict[str, dict[str, Any]] = {}
        for source, kind in ((intra, "intra_exchange_instance"), (subaccount, "subaccount")):
            for row in source.get("transfers", []):
                if not isinstance(row, dict):
                    continue
                item = {**row, "transfer_type": kind}
                key = str(item.get("transfer_id") or "")
                if key:
                    rows[key] = item
        complete = (intra.get("pagination", {}).get("complete") is True
                    and subaccount.get("pagination", {}).get("complete") is True)
        return {"transfers": list(rows.values()), "pagination": {
            "complete": complete,
            "intra_exchange_instance": intra.get("pagination", {}),
            "subaccount": subaccount.get("pagination", {}),
            "records": {"transfers": len(rows)},
        }}

    async def _get_all_pages(
        self, endpoint: str, collection: str, *, params: dict[str, Any],
        limit: int, additional_collections: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Read every official cursor page; fail instead of returning a subtotal."""
        rows: dict[str, list[Any]] = {key: [] for key in (collection, *additional_collections)}
        cursor: str | None = None
        seen: set[str] = set()
        pages = 0
        while True:
            query = {**params, "limit": limit}
            if cursor is not None:
                if cursor in seen:
                    raise ValueError(f"{endpoint} returned a repeated pagination cursor")
                seen.add(cursor)
                query["cursor"] = cursor
            payload = await self._get(endpoint, params=query)
            if not isinstance(payload, dict):
                raise TypeError(f"{endpoint} returned malformed paginated data")
            for key, accumulated in rows.items():
                page_rows = payload.get(key)
                if not isinstance(page_rows, list):
                    raise TypeError(f"{endpoint} page is missing its {key} array")
                accumulated.extend(page_rows)
            pages += 1
            next_cursor = payload.get("cursor")
            if not next_cursor:
                break
            if not isinstance(next_cursor, str) or pages >= 10000:
                raise ValueError(f"{endpoint} pagination did not terminate safely")
            cursor = next_cursor
        return {**rows, "cursor": None, "pagination": {
            "complete": True, "pages": pages,
            "records": {key: len(value) for key, value in rows.items()},
        }}

    async def limits(self) -> dict[str, Any]:
        return await self._get("/account/limits")

    async def user_data_timestamp(self) -> datetime | None:
        response = await self.client.get("/exchange/user_data_timestamp")
        response.raise_for_status()
        raw = response.json().get("as_of_time")
        if not raw:
            return None
        value = datetime.fromisoformat(str(raw))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def snapshot(
        self, *, min_ts: int | None = None, full_history: bool = True,
    ) -> AccountSnapshot:
        async def optional(awaitable: Any, collection: str) -> dict[str, Any]:
            try:
                return await awaitable
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                return {collection: [], "pagination": {
                    "complete": False, "pages": 0, "records": {collection: 0},
                    "error_type": type(exc).__name__,
                }}

        balance, positions, orders, fills, limits, as_of, settlements, deposits, withdrawals, transfers = await asyncio.gather(
            self.balance(),
            self.positions(),
            self.orders(),
            self.fills(min_ts=None if full_history else min_ts),
            self.limits(),
            self.user_data_timestamp(),
            optional(self.settlements(min_ts=None if full_history else min_ts), "settlements"),
            optional(self.deposits(), "deposits"),
            optional(self.withdrawals(), "withdrawals"),
            optional(self.transfers(), "transfers"),
        )
        try:
            historical_fills = await self.historical_fills() if full_history else {
                "fills": [], "pagination": {"complete": True, "pages": 0,
                                               "records": {"fills": 0}, "incremental": True},
            }
            # The historical endpoint holds pre-cutoff fills. It is only
            # revisited on initial sync and scheduled coverage audits.
            seen = {str(row.get("fill_id")) for row in historical_fills["fills"]
                    if isinstance(row, dict) and row.get("fill_id") is not None}
            fills["fills"] = historical_fills["fills"] + [
                row for row in fills["fills"]
                if not isinstance(row, dict) or str(row.get("fill_id")) not in seen
            ]
            fills["pagination"] = {
                "complete": (fills.get("pagination", {}).get("complete") is True
                             and historical_fills.get("pagination", {}).get("complete") is True),
                "incremental": not full_history,
                "live": fills.get("pagination"),
                "historical": historical_fills.get("pagination"),
                "records": {"fills": len(fills["fills"])},
            }
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            fills["pagination"] = {
                "complete": False, "live": fills.get("pagination"),
                "historical": {"complete": False, "error_type": type(exc).__name__},
                "records": {"fills": len(fills.get("fills", []))},
            }
        return AccountSnapshot(
            balance=balance,
            positions=positions,
            orders=orders,
            fills=fills,
            settlements=settlements,
            deposits=deposits,
            withdrawals=withdrawals,
            transfers=transfers,
            limits=limits,
            user_data_as_of=as_of,
        )
