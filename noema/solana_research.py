from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
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


class ProviderFailure(RuntimeError):
    """Safe provider error containing classification only, never endpoint details."""

    def __init__(
        self, provider: str, operation: str, error_class: str, *,
        http_status: int | None = None, attempts: int | None = None,
        elapsed_ms: int | None = None,
    ):
        self.provider = provider
        self.operation = operation
        self.error_class = error_class
        self.http_status = http_status
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms
        super().__init__(f"{provider}:{operation}:{error_class}")

    def provenance(self) -> dict[str, Any]:
        """Return provider diagnostics without endpoint, response-body, or credential data."""
        return {
            "source_alias": self.provider,
            "operation": self.operation,
            "error_class": self.error_class,
            "http_status": self.http_status,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
        }


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        value = response.headers.get("retry-after")
        if value:
            try:
                return min(5.0, max(0.0, float(value)))
            except ValueError:
                try:
                    until = parsedate_to_datetime(value)
                    if until.tzinfo is not None:
                        return min(5.0, max(0.0, (until - datetime.now(UTC)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
    # Small process-local jitter prevents synchronized retries from multiple
    # read-only collectors while keeping the delay bounded and provider-safe.
    base = min(2.0, 0.2 * (2 ** attempt))
    return base * random.uniform(0.8, 1.2)


def _http_error_class(status: int) -> tuple[str, bool]:
    if status == 429:
        return "rate_limited", True
    if status in {408, 425}:
        return "temporary_http_failure", True
    if status >= 500:
        return "provider_server_error", True
    return "provider_http_rejected", False


MAX_RPC_CONTEXT_LAG_SLOTS = 150


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


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
        parsed = datetime.fromisoformat(str(value))
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
    token_program = (
        str(token["tokenProgram"])
        if token.get("tokenProgram") is not None
        else None
    )
    token_2022 = token_program is not None and "2022" in token_program.lower()
    extension_unknown = token_program is None or token_2022

    return TokenControlState(
        token_program=token_program,
        mint_authority_present=_authority_present(audit, "mintAuthorityDisabled"),
        freeze_authority_present=_authority_present(audit, "freezeAuthorityDisabled"),
        permanent_delegate_present=(
            bool(audit["permanentDelegate"])
            if isinstance(audit.get("permanentDelegate"), bool)
            else None if extension_unknown else False
        ),
        # Standard SPL Token cannot use Token-2022-only transfer extensions.
        # For Token-2022, the Tokens V2 payload alone does not establish their state.
        transfer_hook_present=None if extension_unknown else False,
        transfer_fee_bps=None if extension_unknown else 0,
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
    if price is None or price <= 0:
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
        buy_volume_usd=(None if _optional_float(stats.get("buyVolume")) is None
                        else max(0.0, _optional_float(stats.get("buyVolume")))),
        sell_volume_usd=(None if _optional_float(stats.get("sellVolume")) is None
                         else max(0.0, _optional_float(stats.get("sellVolume")))),
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
        fallback_rpc_url: str | None = None,
        timeout_seconds: float = 10.0,
        retry_attempts: int = 3,
    ) -> None:
        self.rpc_url = rpc_url
        self.fallback_rpc_url = fallback_rpc_url
        self.timeout = timeout_seconds
        if retry_attempts < 1 or retry_attempts > 5:
            raise ValueError("retry_attempts must be between one and five")
        self.retry_attempts = retry_attempts
        self._request_id = 0
        self.last_provider: str | None = None
        self.last_error_class: str | None = None
        self.last_context_slot: int | None = None
        self.last_attempt_provenance: dict[str, Any] | None = None
        self.last_request_elapsed_ms: int | None = None
        self.provider_cooldowns: dict[str, datetime] = {}
        self._http_client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def providers(self) -> tuple[tuple[str, str], ...]:
        configured = [("primary", self.rpc_url)]
        fallback = self.fallback_rpc_url
        if fallback and fallback.rstrip("/") != self.rpc_url.rstrip("/"):
            configured.append(("fallback", fallback))
        return tuple(configured)

    async def _rpc(
        self, method: str, params: list[Any], *, endpoint: tuple[str, str] | None = None,
    ) -> Any:
        provider, url = endpoint or self.providers()[0]
        started = time.monotonic()
        last_error: ProviderFailure | None = None
        for attempt in range(self.retry_attempts):
            self._request_id += 1
            request_id = self._request_id
            payload = {
                "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
            }
            response: httpx.Response | None = None
            try:
                if self._http_client is None or self._http_client.is_closed:
                    self._http_client = httpx.AsyncClient(
                        timeout=self.timeout, follow_redirects=False,
                    )
                response = await self._http_client.post(url, json=payload)
                if response.is_error:
                    error_class, retryable = _http_error_class(response.status_code)
                    raise ProviderFailure(
                        provider, method, error_class,
                        http_status=response.status_code,
                    )
                data = response.json()
            except ProviderFailure as exc:
                last_error = exc
                retryable = exc.error_class in {
                    "rate_limited", "temporary_http_failure", "provider_server_error",
                }
            except httpx.TimeoutException:
                last_error = ProviderFailure(provider, method, "timeout")
                retryable = True
            except httpx.TransportError:
                last_error = ProviderFailure(provider, method, "transport_error")
                retryable = True
            except (ValueError, TypeError):
                last_error = ProviderFailure(provider, method, "malformed_json")
                retryable = False
            else:
                if (not isinstance(data, dict) or data.get("jsonrpc") != "2.0"
                        or data.get("id") != request_id):
                    last_error = ProviderFailure(provider, method, "invalid_rpc_envelope")
                    retryable = False
                elif data.get("error"):
                    last_error = ProviderFailure(provider, method, "rpc_method_error")
                    retryable = False
                else:
                    result = data.get("result")
                    if not isinstance(result, dict) and type(result) is not int:
                        last_error = ProviderFailure(provider, method, "missing_result")
                        retryable = False
                    else:
                        self.last_provider = provider
                        self.last_error_class = None
                        self.last_request_elapsed_ms = round((time.monotonic() - started) * 1000)
                        return result
            if not retryable or attempt + 1 >= self.retry_attempts:
                break
            await asyncio.sleep(_retry_delay(response, attempt))
        self.last_error_class = last_error.error_class if last_error else "provider_failure"
        error = last_error or ProviderFailure(provider, method, "provider_failure")
        raise ProviderFailure(
            error.provider, error.operation, error.error_class,
            http_status=error.http_status, attempts=attempt + 1,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        ) from error

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
        errors: list[ProviderFailure] = []
        started = time.monotonic()
        self.last_attempt_provenance = None
        self.last_context_slot = None
        for endpoint in self.providers():
            cooldown = self.provider_cooldowns.get(endpoint[0])
            if cooldown is not None and datetime.now(UTC) < cooldown:
                errors.append(ProviderFailure(endpoint[0], "holder_enrichment", "backoff_active"))
                continue
            try:
                current_slot = await self._rpc(
                    "getSlot", [{"commitment": "confirmed"}], endpoint=endpoint,
                )
                if type(current_slot) is not int or current_slot <= 0:
                    raise ProviderFailure(endpoint[0], "getSlot", "invalid_slot")
                supply_result = await self._rpc(
                    "getTokenSupply", [mint, {"commitment": "confirmed"}], endpoint=endpoint,
                )
                supply_context = supply_result.get("context")
                supply_slot = supply_context.get("slot") if isinstance(supply_context, dict) else None
                supply_value = supply_result.get("value")
                if (type(supply_slot) is not int or supply_slot <= 0
                        or not isinstance(supply_value, dict)):
                    raise ProviderFailure(endpoint[0], "getTokenSupply", "invalid_context_or_value")
                if current_slot - supply_slot > MAX_RPC_CONTEXT_LAG_SLOTS:
                    raise ProviderFailure(endpoint[0], "getTokenSupply", "stale_context")
                supply = TokenSupply(
                    raw_amount=int(supply_value["amount"]),
                    decimals=int(supply_value["decimals"]),
                    ui_amount_string=str(supply_value["uiAmountString"]),
                )
                if supply.raw_amount <= 0:
                    raise ProviderFailure(endpoint[0], "getTokenSupply", "invalid_supply")
                accounts_result = await self._rpc(
                    "getTokenLargestAccounts",
                    [mint, {"commitment": "confirmed"}],
                    endpoint=endpoint,
                )
                account_context = accounts_result.get("context")
                account_slot = account_context.get("slot") if isinstance(account_context, dict) else None
                values = accounts_result.get("value")
                if (type(account_slot) is not int or account_slot < supply_slot
                        or not isinstance(values, list)):
                    raise ProviderFailure(endpoint[0], "getTokenLargestAccounts", "stale_or_invalid_context")
                if current_slot - account_slot > MAX_RPC_CONTEXT_LAG_SLOTS:
                    raise ProviderFailure(endpoint[0], "getTokenLargestAccounts", "stale_context")
                accounts = tuple(
                    LargestTokenAccount(
                        address=str(item["address"]), raw_amount=int(item["amount"]),
                        decimals=int(item["decimals"]),
                    )
                    for item in values if isinstance(item, dict)
                )
                if not accounts or any(
                    account.raw_amount < 0 or account.decimals != supply.decimals
                    for account in accounts
                ):
                    raise ProviderFailure(endpoint[0], "getTokenLargestAccounts", "invalid_accounts")
                self.last_provider = endpoint[0]
                self.last_context_slot = account_slot
                self.last_error_class = None
                self.last_attempt_provenance = {
                    "source_alias": endpoint[0],
                    "fallback_used": endpoint[0] == "fallback",
                    "context_slot": account_slot,
                    "observed_slot": current_slot,
                    "freshness_slots": current_slot - account_slot,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "failed_providers": [failure.provenance() for failure in errors],
                }
                return tuple(min(1.0, account.raw_amount / supply.raw_amount) for account in accounts)
            except (ProviderFailure, KeyError, ValueError, TypeError, OverflowError) as exc:
                failure = exc if isinstance(exc, ProviderFailure) else ProviderFailure(
                    endpoint[0], "holder_enrichment", "invalid_provider_data",
                )
                errors.append(failure)
                self.last_error_class = failure.error_class
        self.last_attempt_provenance = {
            "source_alias": None,
            "fallback_used": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "failed_providers": [failure.provenance() for failure in errors],
        }
        if errors:
            raise errors[-1]
        raise ProviderFailure("solana_rpc", "holder_enrichment", "no_provider_configured")


class JupiterTrenchResearchClient:
    """Read-only Jupiter token discovery signals for Trench-1."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.jup.ag",
        timeout_seconds: float = 10.0,
        retry_attempts: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        if retry_attempts < 1 or retry_attempts > 5:
            raise ValueError("retry_attempts must be between one and five")
        self.retry_attempts = retry_attempts
        self.headers = {"x-api-key": api_key} if api_key else {}
        self.last_error_class: str | None = None

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        operation = path.rsplit("/", 1)[-1]
        for attempt in range(self.retry_attempts):
            response: httpx.Response | None = None
            try:
                async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                    response = await client.get(f"{self.base_url}{path}", params=params)
                    if response.is_error:
                        error_class, retryable = _http_error_class(response.status_code)
                        raise ProviderFailure("jupiter", operation, error_class)
                    payload = response.json()
            except ProviderFailure as exc:
                failure = exc
                retryable = failure.error_class in {
                    "rate_limited", "temporary_http_failure", "provider_server_error",
                }
            except httpx.TimeoutException:
                failure = ProviderFailure("jupiter", operation, "timeout")
                retryable = True
            except httpx.TransportError:
                failure = ProviderFailure("jupiter", operation, "transport_error")
                retryable = True
            except (ValueError, TypeError):
                failure = ProviderFailure("jupiter", operation, "malformed_json")
                retryable = False
            else:
                self.last_error_class = None
                return payload
            self.last_error_class = failure.error_class
            if not retryable or attempt + 1 >= self.retry_attempts:
                raise failure
            await asyncio.sleep(_retry_delay(response, attempt))
        raise ProviderFailure("jupiter", operation, "provider_failure")

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


class DexScreenerTrenchPriceClient:
    """Independent read-only fallback for current Solana token price/liquidity."""

    def __init__(self, *, timeout_seconds: float = 8.0) -> None:
        self.timeout = timeout_seconds
        self.base_url = "https://api.dexscreener.com"

    async def tokens_by_mint(self, mints: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
        clean = tuple(dict.fromkeys(mint.strip() for mint in mints if mint.strip()))
        if not clean:
            return {}
        if len(clean) > 30:
            raise ValueError("DexScreener token lookup supports at most 30 mints per request")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/tokens/v1/solana/{','.join(clean)}"
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            error_class, _ = _http_error_class(exc.response.status_code)
            raise ProviderFailure("dexscreener", "tokens_by_mint", error_class,
                                  http_status=exc.response.status_code) from exc
        except httpx.TimeoutException as exc:
            raise ProviderFailure("dexscreener", "tokens_by_mint", "timeout") from exc
        except httpx.TransportError as exc:
            raise ProviderFailure("dexscreener", "tokens_by_mint", "transport_error") from exc
        except (ValueError, TypeError) as exc:
            raise ProviderFailure("dexscreener", "tokens_by_mint", "malformed_json") from exc
        if not isinstance(payload, list):
            raise ProviderFailure("dexscreener", "tokens_by_mint", "invalid_response")

        selected: dict[str, dict[str, Any]] = {}
        for pair in payload:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            base = pair.get("baseToken")
            if not isinstance(base, dict):
                continue
            mint = base.get("address")
            if not isinstance(mint, str) or mint not in clean:
                continue
            price = _optional_float(pair.get("priceUsd"))
            liquidity_obj = pair.get("liquidity")
            liquidity = (_optional_float(liquidity_obj.get("usd"))
                         if isinstance(liquidity_obj, dict) else None)
            transactions = pair.get("txns")
            recent = transactions.get("m5") if isinstance(transactions, dict) else None
            recent_trades = (int(recent.get("buys", 0) or 0) + int(recent.get("sells", 0) or 0)
                             if isinstance(recent, dict) else 0)
            if price is None or price <= 0 or liquidity is None or liquidity < 0 or recent_trades <= 0:
                continue
            prior = selected.get(mint)
            prior_liquidity = (prior or {}).get("liquidity", {}).get("usd", -1)
            if prior is None or liquidity > float(prior_liquidity):
                selected[mint] = pair
        return selected
