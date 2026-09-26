from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .trench_models import LaunchTick, TokenControlState


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


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _authority_present(audit: dict[str, Any], key: str) -> bool | None:
    disabled = audit.get(key)
    if disabled is True:
        return False
    if disabled is False:
        return True
    return None


def parse_jupiter_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        # Jupiter timestamp fields may be milliseconds since epoch.
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def jupiter_first_pool_at(token: dict[str, Any]) -> datetime | None:
    pool = token.get("firstPool")
    if isinstance(pool, dict):
        for key in ("createdAt", "created_at", "createdAtMs"):
            parsed = parse_jupiter_time(pool.get(key))
            if parsed is not None:
                return parsed
    for key in ("firstPoolCreatedAt", "createdAt"):
        parsed = parse_jupiter_time(token.get(key))
        if parsed is not None:
            return parsed
    return None


def jupiter_control_state(token: dict[str, Any]) -> TokenControlState:
    audit = token.get("audit")
    audit = audit if isinstance(audit, dict) else {}

    suspicious = bool(audit.get("isSus") is True)
    return TokenControlState(
        token_program=(
            str(token["tokenProgram"])
            if token.get("tokenProgram") is not None
            else None
        ),
        mint_authority_present=_authority_present(audit, "mintAuthorityDisabled"),
        freeze_authority_present=_authority_present(audit, "freezeAuthorityDisabled"),
        permanent_delegate_present=(
            bool(audit["permanentDelegate"])
            if isinstance(audit.get("permanentDelegate"), bool)
            else None
        ),
        # Tokens V2 does not establish these extension details by itself.
        transfer_hook_present=None,
        transfer_fee_bps=None,
        suspicious_flag=suspicious,
    )


def jupiter_launch_tick(
    token: dict[str, Any],
    *,
    observed_at: datetime,
    holder_shares: tuple[float, ...] = (),
) -> LaunchTick:
    """Normalize a Jupiter Tokens V2 response for a launch younger than 24h.

    For such launches stats24h is a useful life-to-date approximation because the token's
    first tradeable pool did not exist before the rolling 24h window. Raw provider payloads
    are stored separately by the collector so this interpretation remains auditable.
    """

    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")

    stats = token.get("stats24h")
    stats = stats if isinstance(stats, dict) else {}
    audit = token.get("audit")
    audit = audit if isinstance(audit, dict) else {}

    first_pool = jupiter_first_pool_at(token)
    if first_pool is not None:
        age_seconds = (observed_at.astimezone(UTC) - first_pool).total_seconds()
        if age_seconds < 0:
            raise ValueError("observation precedes first pool")
        if age_seconds > 86_400:
            raise ValueError("stats24h launch normalization only supports <=24h tokens")

    price = _optional_float(token.get("usdPrice"))
    liquidity = _optional_float(token.get("liquidity"))
    if price is None or price < 0:
        raise ValueError("Jupiter token price unavailable")
    if liquidity is None or liquidity < 0:
        raise ValueError("Jupiter token liquidity unavailable")

    dev_balance_pct = _optional_float(audit.get("devBalancePercentage"))
    creator_fraction = (
        None
        if dev_balance_pct is None
        else max(0.0, min(1.0, dev_balance_pct / 100.0))
    )

    return LaunchTick(
        observed_at=observed_at.astimezone(UTC),
        price_usd=price,
        liquidity_usd=liquidity,
        buy_volume_usd=max(0.0, _optional_float(stats.get("buyVolume")) or 0.0),
        sell_volume_usd=max(0.0, _optional_float(stats.get("sellVolume")) or 0.0),
        organic_net_buyers=_optional_int(stats.get("numOrganicBuyers")),
        total_traders=_optional_int(stats.get("numTraders")),
        net_buyers=_optional_int(stats.get("numNetBuyers")),
        organic_buy_volume_usd=_optional_float(stats.get("buyOrganicVolume")),
        organic_sell_volume_usd=_optional_float(stats.get("sellOrganicVolume")),
        holder_shares=holder_shares,
        creator_supply_fraction=creator_fraction,
        organic_score=_optional_float(token.get("organicScore")),
        wash_trade_probability=None,
    )


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

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
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

    async def tokens_by_mint(
        self,
        mints: list[str] | tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        clean = tuple(dict.fromkeys(mint.strip() for mint in mints if mint.strip()))
        if not clean:
            return {}
        if len(clean) > 100:
            raise ValueError("Jupiter token search supports at most 100 mints per request")
        data = await self._get(
            "/tokens/v2/search",
            params={"query": ",".join(clean)},
        )
        if not isinstance(data, list):
            raise TypeError("unexpected Jupiter token-search response")
        result: dict[str, dict[str, Any]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            mint = item.get("id") or item.get("address")
            if isinstance(mint, str) and mint in clean:
                result[mint] = item
        return result
