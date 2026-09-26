from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class TokenSupply:
    raw_amount: int
    decimals: int
    ui_amount_string: str


@dataclass(frozen=True)
class LargestTokenAccount:
    address: str
    raw_amount: int
    decimals: int


class SolanaRpcResearchClient:
    """Minimal read-only RPC client for token concentration research."""

    def __init__(
        self,
        *,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.timeout = timeout_seconds
        self._request_id = 0

    async def _rpc(self, method: str, params: list[Any]) -> dict[str, Any]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("error"):
            raise RuntimeError(f"Solana RPC error: {data['error']}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise TypeError("Solana RPC response missing result")
        return result

    async def token_supply(self, mint: str) -> TokenSupply:
        result = await self._rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
        value = result.get("value") or {}
        return TokenSupply(
            raw_amount=int(value["amount"]),
            decimals=int(value["decimals"]),
            ui_amount_string=str(value["uiAmountString"]),
        )

    async def largest_token_accounts(
        self,
        mint: str,
    ) -> tuple[LargestTokenAccount, ...]:
        result = await self._rpc(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
        )
        values = result.get("value") or []
        return tuple(
            LargestTokenAccount(
                address=str(item["address"]),
                raw_amount=int(item["amount"]),
                decimals=int(item["decimals"]),
            )
            for item in values
        )

    async def top_account_supply_shares(self, mint: str) -> tuple[float, ...]:
        supply = await self.token_supply(mint)
        if supply.raw_amount <= 0:
            return ()
        accounts = await self.largest_token_accounts(mint)
        return tuple(
            min(1.0, account.raw_amount / supply.raw_amount)
            for account in accounts
        )


class JupiterTrenchResearchClient:
    """Read-only Jupiter token discovery signals for Trench-1."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.jup.ag",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.headers = {"x-api-key": api_key} if api_key else {}

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            response = await client.get(f"{self.base_url}{path}")
            response.raise_for_status()
            return response.json()

    async def recent_tradeable_tokens(self) -> list[dict[str, Any]]:
        data = await self._get("/tokens/v2/recent")
        if not isinstance(data, list):
            raise TypeError("unexpected Jupiter recent-token response")
        return [item for item in data if isinstance(item, dict)]

    async def top_organic_tokens_5m(self) -> list[dict[str, Any]]:
        data = await self._get("/tokens/v2/toporganicscore/5m")
        if not isinstance(data, list):
            raise TypeError("unexpected Jupiter organic-score response")
        return [item for item in data if isinstance(item, dict)]
