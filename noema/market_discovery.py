"""Bounded, read-only discovery of provider-observed DEX pairs.

Discovery creates research candidates only. It does not validate contract safety,
approve an asset, request a quote, or grant execution authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .chain_registry import resolve_provider_network

_BASE = "https://api.dexscreener.com"
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58 = frozenset(_BASE58_ALPHABET)
_SAFE_PROVIDER_ID = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_SAFE_PROVIDER_ADDRESS = re.compile(r"^[a-zA-Z0-9._:-]{1,160}$")
_MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024


def _valid_solana_pubkey(address: str) -> bool:
    if not 32 <= len(address) <= 44 or any(char not in _BASE58 for char in address):
        return False
    number = 0
    for char in address:
        number = number * 58 + _BASE58_ALPHABET.index(char)
    decoded_bytes = (number.bit_length() + 7) // 8
    leading_zeroes = len(address) - len(address.lstrip("1"))
    return decoded_bytes + leading_zeroes == 32


class MarketDiscoveryError(RuntimeError):
    """Safe provider failure; does not retain URLs or response bodies."""

    def __init__(self, operation: str, error_class: str, http_status: int | None = None):
        self.operation = operation
        self.error_class = error_class
        self.http_status = http_status
        super().__init__(f"dexscreener:{operation}:{error_class}"
                         + (f":{http_status}" if http_status is not None else ""))


@dataclass(frozen=True)
class MarketDiscoveryConfig:
    enabled: bool = False
    interval_seconds: int = 900
    token_limit: int = 8
    pair_limit_per_token: int = 5

    @classmethod
    def from_env(cls) -> MarketDiscoveryConfig:
        enabled = os.getenv("NOEMA_MARKET_DISCOVERY_ENABLED", "0").strip().lower()
        interval = _int_env("NOEMA_MARKET_DISCOVERY_INTERVAL_SECONDS", 900)
        token_limit = _int_env("NOEMA_MARKET_DISCOVERY_TOKEN_LIMIT", 8)
        pair_limit = _int_env("NOEMA_MARKET_DISCOVERY_PAIR_LIMIT_PER_TOKEN", 5)
        config = cls(enabled in {"1", "true", "yes", "on"}, interval, token_limit, pair_limit)
        config.validate()
        return config

    def validate(self) -> None:
        if not 300 <= self.interval_seconds <= 3600:
            raise ValueError("market discovery interval must be within [300, 3600] seconds")
        if not 1 <= self.token_limit <= 20:
            raise ValueError("market discovery token limit must be within [1, 20]")
        if not 1 <= self.pair_limit_per_token <= 20:
            raise ValueError("market discovery pair limit must be within [1, 20]")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class DiscoveredPairObservation:
    observation_id: str
    provider: str
    observed_at: str
    provider_chain_id: str
    canonical_network_id: str | None
    chain_family: str | None
    pair_address: str
    pair_id: str
    base_asset_id: str | None
    base_address: str
    base_symbol: str | None
    base_name: str | None
    quote_asset_id: str | None
    quote_address: str
    quote_symbol: str | None
    quote_name: str | None
    dex_id: str | None
    price_usd: str | None
    price_native: str | None
    liquidity_usd: str | None
    volume: dict[str, str]
    transactions: dict[str, dict[str, int]]
    pair_created_at: str | None
    source_timestamp_semantics: str
    source_payload_sha256: str
    evidence_state: str
    candidate_state: str
    execution_authority_state: str = "disabled"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _valid_provider_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result if _SAFE_PROVIDER_ID.fullmatch(result) else None


def _safe_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    if not result or len(result) > 96 or any(ord(char) < 32 for char in result):
        return None
    return result


def _asset_address(
    value: object, chain_id: str,
) -> tuple[str, str | None, str | None, str | None] | None:
    if not isinstance(value, str):
        return None
    address = value.strip()
    if not address or any(ord(char) < 32 for char in address):
        return None
    network = resolve_provider_network(chain_id)
    family = network.chain_family if network is not None else None
    canonical_network_id = network.canonical_network_id if network is not None else None
    if family == "evm" and canonical_network_id is not None:
        if not _EVM_ADDRESS.fullmatch(address):
            return None
        return address.lower(), f"{canonical_network_id}/erc20:{address.lower()}", canonical_network_id, family
    if family == "solana" and canonical_network_id is not None:
        if not _valid_solana_pubkey(address):
            return None
        return address, f"{canonical_network_id}/token:{address}", canonical_network_id, family
    # Unknown source network slugs remain provider-scoped candidates. We do not
    # infer a family, chain ID, or canonical asset identity from a ticker.
    if not _SAFE_PROVIDER_ADDRESS.fullmatch(address):
        return None
    return address, None, canonical_network_id, family


def _decimal_string(value: object, *, positive: bool = False) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        return None
    return format(number, "f")


def _timestamp_ms(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        millis = int(value)
        if millis <= 0:
            return None
        return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _safe_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, count in value.items():
        name = _valid_provider_id(key)
        if name is not None and isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[name] = count
    return result


def normalize_pair(
    pair: object, *, observed_at: datetime, discovered_chain_id: str,
    discovered_token_address: str,
) -> DiscoveredPairObservation | None:
    """Normalize one provider pair only when it includes the discovered token."""
    if not isinstance(pair, dict):
        return None
    chain_value = _valid_provider_id(pair.get("chainId"))
    chain_id = None if chain_value is None else chain_value.lower()
    pair_address = pair.get("pairAddress")
    if chain_id is None or chain_id.lower() != discovered_chain_id.lower():
        return None
    if not isinstance(pair_address, str) or not _SAFE_PROVIDER_ADDRESS.fullmatch(pair_address.strip()):
        return None
    pair_address = pair_address.strip()
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
    base_raw, quote_raw = base.get("address"), quote.get("address")
    base_normalized = _asset_address(base_raw, chain_id)
    quote_normalized = _asset_address(quote_raw, chain_id)
    discovered = _asset_address(discovered_token_address, chain_id)
    if base_normalized is None or quote_normalized is None or discovered is None:
        return None
    if discovered[0].lower() not in {base_normalized[0].lower(), quote_normalized[0].lower()}:
        return None
    if observed_at.tzinfo is None:
        raise ValueError("market discovery observation timestamp must be timezone-aware")
    observed = observed_at.astimezone(UTC)
    dex_id = _valid_provider_id(pair.get("dexId"))
    price_usd = _decimal_string(pair.get("priceUsd"), positive=True)
    price_native = _decimal_string(pair.get("priceNative"), positive=True)
    liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    liquidity_usd = _decimal_string(liquidity.get("usd"))
    raw_volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    volume = {
        safe_key: value for raw_key, raw in raw_volume.items()
        if (safe_key := _valid_provider_id(raw_key)) is not None
        and (value := _decimal_string(raw)) is not None
    }
    raw_txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    transactions = {
        key: counts for timeframe, raw in raw_txns.items()
        if (key := _valid_provider_id(timeframe)) is not None
        and (counts := _safe_counts(raw))
    }
    canonical_network_id = base_normalized[2] if base_normalized[2] == quote_normalized[2] else None
    family = base_normalized[3] if base_normalized[3] == quote_normalized[3] else None
    if family == "evm" and not _EVM_ADDRESS.fullmatch(pair_address):
        return None
    if family == "solana" and (
        not _valid_solana_pubkey(pair_address)
    ):
        return None
    normalized_pair_address = pair_address.lower() if family == "evm" else pair_address
    pair_id = (
        f"{canonical_network_id}/dex-pair:{normalized_pair_address}"
        if canonical_network_id else f"dexscreener:{chain_id}/{normalized_pair_address}"
    )
    safe_evidence = {
        "chainId": chain_id,
        "pairAddress": normalized_pair_address,
        "baseToken": {"address": base_normalized[0], "symbol": base.get("symbol"), "name": base.get("name")},
        "quoteToken": {"address": quote_normalized[0], "symbol": quote.get("symbol"), "name": quote.get("name")},
        "dexId": dex_id,
        "priceUsd": price_usd,
        "priceNative": price_native,
        "liquidityUsd": liquidity_usd,
        "volume": volume,
        "transactions": transactions,
        "pairCreatedAt": pair.get("pairCreatedAt"),
    }
    payload_hash = hashlib.sha256(
        json.dumps(safe_evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    digest_input = f"dexscreener|{chain_id}|{normalized_pair_address}|{observed.isoformat()}|{payload_hash}"
    observation_id = hashlib.sha256(digest_input.encode()).hexdigest()
    return DiscoveredPairObservation(
        observation_id=observation_id,
        provider="dexscreener",
        observed_at=observed.isoformat(),
        provider_chain_id=chain_id,
        canonical_network_id=canonical_network_id,
        chain_family=family,
        pair_address=normalized_pair_address,
        pair_id=pair_id,
        base_asset_id=base_normalized[1],
        base_address=base_normalized[0],
        base_symbol=_safe_label(base.get("symbol")),
        base_name=_safe_label(base.get("name")),
        quote_asset_id=quote_normalized[1],
        quote_address=quote_normalized[0],
        quote_symbol=_safe_label(quote.get("symbol")),
        quote_name=_safe_label(quote.get("name")),
        dex_id=dex_id,
        price_usd=price_usd,
        price_native=price_native,
        liquidity_usd=liquidity_usd,
        volume=volume,
        transactions=transactions,
        pair_created_at=_timestamp_ms(pair.get("pairCreatedAt")),
        source_timestamp_semantics="collector_observed; provider quote timestamp unavailable",
        source_payload_sha256=payload_hash,
        evidence_state="provider_observed_unverified",
        candidate_state="unreviewed",
    )


class DexScreenerDiscoveryClient:
    """Polled public profile and token-pair endpoints with a strict request cap."""

    def __init__(self, *, client: httpx.AsyncClient | None = None, timeout_seconds: float = 8.0):
        self._client = client
        self._timeout = timeout_seconds

    async def discover(
        self, *, token_limit: int = 8, pair_limit_per_token: int = 5,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not 1 <= token_limit <= 20 or not 1 <= pair_limit_per_token <= 20:
            raise ValueError("market discovery bounds are outside supported limits")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        observed_at = now or datetime.now(UTC)
        if observed_at.tzinfo is None:
            raise ValueError("market discovery timestamp must be timezone-aware")
        try:
            profiles = await self._get_json(client, "profiles", f"{_BASE}/token-profiles/latest/v1")
            if not isinstance(profiles, list):
                raise MarketDiscoveryError("profiles", "invalid_response")
            unique: dict[tuple[str, str], None] = {}
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                chain_value = _valid_provider_id(profile.get("chainId"))
                chain = None if chain_value is None else chain_value.lower()
                address = profile.get("tokenAddress")
                if chain and isinstance(address, str) and _asset_address(address, chain):
                    unique.setdefault((chain, address.strip()), None)
                if len(unique) >= token_limit:
                    break
            observations: list[DiscoveredPairObservation] = []
            failed_requests = 0
            rejected_pairs = 0
            for chain, address in unique:
                path_chain = quote(chain, safe="")
                path_address = quote(address, safe="")
                try:
                    rows = await self._get_json(
                        client, "token_pairs", f"{_BASE}/token-pairs/v1/{path_chain}/{path_address}"
                    )
                except MarketDiscoveryError:
                    failed_requests += 1
                    continue
                if not isinstance(rows, list):
                    failed_requests += 1
                    continue
                for pair in rows[:pair_limit_per_token]:
                    normalized = normalize_pair(
                        pair, observed_at=observed_at, discovered_chain_id=chain,
                        discovered_token_address=address,
                    )
                    if normalized is None:
                        rejected_pairs += 1
                    else:
                        observations.append(normalized)
            return {
                "status": "partial" if failed_requests else "ok",
                "profiles_seen": len(profiles),
                "tokens_queried": len(unique),
                "pairs_observed": len(observations),
                "pairs_rejected": rejected_pairs,
                "failed_requests": failed_requests,
                "observations": observations,
            }
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    async def _get_json(client: httpx.AsyncClient, operation: str, url: str) -> Any:
        try:
            response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise MarketDiscoveryError(operation, "timeout") from exc
        except httpx.HTTPError as exc:
            raise MarketDiscoveryError(operation, "transport_failure") from exc
        if response.status_code == 429:
            raise MarketDiscoveryError(operation, "rate_limited", 429)
        if response.status_code >= 500:
            raise MarketDiscoveryError(operation, "provider_server_error", response.status_code)
        if response.status_code >= 400:
            raise MarketDiscoveryError(operation, "provider_rejected", response.status_code)
        if len(response.content) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise MarketDiscoveryError(operation, "response_too_large", response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise MarketDiscoveryError(operation, "invalid_json", response.status_code) from exc


class MarketDiscoveryStore:
    """Append-only provider observations plus a non-authoritative candidate index."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(db)
        with sqlite3.connect(self.path, timeout=5.0) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS market_discovery_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    provider_chain_id TEXT NOT NULL,
                    canonical_network_id TEXT,
                    pair_address TEXT NOT NULL,
                    pair_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    evidence_state TEXT NOT NULL,
                    candidate_state TEXT NOT NULL,
                    execution_authority_state TEXT NOT NULL CHECK(execution_authority_state='disabled'),
                    UNIQUE(provider, provider_chain_id, pair_address)
                );
                CREATE TABLE IF NOT EXISTS market_pair_observations (
                    observation_id TEXT PRIMARY KEY,
                    candidate_id INTEGER NOT NULL REFERENCES market_discovery_candidates(id),
                    observed_at TEXT NOT NULL,
                    source_payload_sha256 TEXT NOT NULL,
                    observation_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_market_pair_observations_candidate_time
                    ON market_pair_observations(candidate_id, observed_at);
                CREATE INDEX IF NOT EXISTS ix_market_candidates_last_seen
                    ON market_discovery_candidates(last_seen_at);
            """)

    def persist(self, observations: list[DiscoveredPairObservation]) -> dict[str, int]:
        inserted = duplicates = 0
        with sqlite3.connect(self.path, timeout=5.0) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            for observation in observations:
                data = observation.as_dict()
                connection.execute(
                    "INSERT INTO market_discovery_candidates "
                    "(provider,provider_chain_id,canonical_network_id,pair_address,pair_id,first_seen_at,last_seen_at,"
                    "evidence_state,candidate_state,execution_authority_state) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(provider,provider_chain_id,pair_address) DO UPDATE SET "
                    "last_seen_at=excluded.last_seen_at",
                    (observation.provider, observation.provider_chain_id,
                     observation.canonical_network_id, observation.pair_address,
                     observation.pair_id, observation.observed_at, observation.observed_at,
                     observation.evidence_state, observation.candidate_state,
                     observation.execution_authority_state),
                )
                candidate_id = connection.execute(
                    "SELECT id FROM market_discovery_candidates WHERE provider=? AND provider_chain_id=? AND pair_address=?",
                    (observation.provider, observation.provider_chain_id, observation.pair_address),
                ).fetchone()[0]
                result = connection.execute(
                    "INSERT OR IGNORE INTO market_pair_observations "
                    "(observation_id,candidate_id,observed_at,source_payload_sha256,observation_json) "
                    "VALUES(?,?,?,?,?)",
                    (observation.observation_id, candidate_id, observation.observed_at,
                     observation.source_payload_sha256,
                     json.dumps(data, sort_keys=True, separators=(",", ":"))),
                )
                if result.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
        return {"candidates_seen": len(observations), "observations_persisted": inserted,
                "duplicates": duplicates}


async def collect_and_persist_market_discovery(
    database: str, *, client: DexScreenerDiscoveryClient | None = None,
    token_limit: int = 8, pair_limit_per_token: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    discovery_client = client or DexScreenerDiscoveryClient()
    result = await discovery_client.discover(
        token_limit=token_limit, pair_limit_per_token=pair_limit_per_token, now=now,
    )
    persisted = MarketDiscoveryStore(database).persist(result.pop("observations"))
    return {**result, **persisted}
