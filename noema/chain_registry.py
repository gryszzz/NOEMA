"""Read-only network registry keyed by canonical EVM chain identity.

RPC URLs are loaded from environment variables but intentionally never exposed
from this module's public projections.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,11}$")
_RPC_ID_KEY = re.compile(r"^NOEMA_EVM_RPC_URL_([1-9][0-9]{0,19})$")


@dataclass(frozen=True)
class EVMChain:
    chain_id: int
    name: str
    native_symbol: str
    rpc_endpoint: str | None = field(repr=False)
    rpc_provider: str
    native_price_symbol: str | None = None
    supported_quote_sources: tuple[str, ...] = ()

    @property
    def canonical_id(self) -> str:
        return f"eip155:{self.chain_id}"

    @property
    def native_asset_id(self) -> str:
        return f"{self.canonical_id}/native"

    def public_record(self) -> dict[str, object]:
        return {
            "canonical_network_id": self.canonical_id,
            "chain_id": self.chain_id,
            "chain": self.name,
            "chain_family": "evm",
            "native_symbol": self.native_symbol,
            "native_asset_id": self.native_asset_id,
            "provider": self.rpc_provider,
            "supported_data_sources": ["evm_json_rpc"],
            "supported_quote_sources": list(self.supported_quote_sources),
            "observed_dexes": [],
            "execution_authority_state": "disabled",
        }


_DEFAULTS: dict[int, tuple[str, str, str, str | None]] = {
    1: ("ethereum", "ETH", "https://ethereum-rpc.publicnode.com", "ETH"),
    8453: ("base", "ETH", "https://mainnet.base.org", "ETH"),
    42161: ("arbitrum", "ETH", "https://arb1.arbitrum.io/rpc", "ETH"),
    10: ("optimism", "ETH", "https://mainnet.optimism.io", "ETH"),
    137: ("polygon", "POL", "https://polygon-bor-rpc.publicnode.com", "POL"),
    56: ("bnb-chain", "BNB", "https://bsc-dataseed.binance.org", "BNB"),
    43114: ("avalanche", "AVAX", "https://api.avax.network/ext/bc/C/rpc", "AVAX"),
}
_LEGACY_RPC_NAMES = {
    1: "ETHEREUM", 8453: "BASE", 42161: "ARBITRUM", 10: "OPTIMISM",
    137: "POLYGON", 56: "BNB", 43114: "AVALANCHE",
}
_ZEROX_QUOTE_CHAIN_IDS = set(_DEFAULTS)
_EVM_PROVIDER_ALIASES: dict[int, tuple[str, ...]] = {
    56: ("bsc",),
    43114: ("avax",),
}
_SOLANA_MAINNET_ID = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
_BITCOIN_MAINNET_ID = "bip122:000000000019d6689c085ae165831e93"


@dataclass(frozen=True)
class NetworkRecord:
    """Family-neutral network identity and read-only capability declaration."""

    canonical_network_id: str
    chain_family: str
    name: str
    native_symbol: str | None
    provider_aliases: tuple[str, ...]
    provider: str
    supported_data_sources: tuple[str, ...] = ()
    supported_quote_sources: tuple[str, ...] = ()
    execution_authority_state: str = "disabled"

    def public_record(self) -> dict[str, object]:
        return {
            "canonical_network_id": self.canonical_network_id,
            "chain_family": self.chain_family,
            "name": self.name,
            "native_symbol": self.native_symbol,
            "provider_aliases": list(self.provider_aliases),
            "provider": self.provider,
            "supported_data_sources": list(self.supported_data_sources),
            "supported_quote_sources": list(self.supported_quote_sources),
            "execution_authority_state": self.execution_authority_state,
        }


def load_network_registry(environ: Mapping[str, str] | None = None) -> tuple[NetworkRecord, ...]:
    """Return EVM, Solana, Bitcoin, and metadata-registered network identities.

    Additional metadata never supplies an RPC URL or signing capability. Unknown
    networks remain unconfigured until an independent read-only provider is set up.
    """
    env = os.environ if environ is None else environ
    records: list[NetworkRecord] = []
    for chain in load_evm_chains(env):
        aliases = tuple(dict.fromkeys((chain.name, *_EVM_PROVIDER_ALIASES.get(chain.chain_id, ()))))
        records.append(NetworkRecord(
            canonical_network_id=chain.canonical_id,
            chain_family="evm",
            name=chain.name,
            native_symbol=chain.native_symbol,
            provider_aliases=aliases,
            provider=chain.rpc_provider,
            supported_data_sources=("evm_json_rpc",),
            supported_quote_sources=chain.supported_quote_sources,
        ))
    solana_configured = bool((env.get("NOEMA_SOLANA_RPC_URL") or "").strip())
    records.append(NetworkRecord(
        canonical_network_id=_SOLANA_MAINNET_ID,
        chain_family="solana",
        name="solana-mainnet",
        native_symbol="SOL",
        provider_aliases=("solana",),
        provider="configured_json_rpc" if solana_configured else "default_public_json_rpc",
        supported_data_sources=("solana_json_rpc", "jupiter_tokens_v2"),
        supported_quote_sources=("jupiter_swap_v2",),
    ))
    bitcoin_configured = bool((env.get("NOEMA_BITCOIN_EXPLORER_URL") or "").strip())
    records.append(NetworkRecord(
        canonical_network_id=_BITCOIN_MAINNET_ID,
        chain_family="utxo",
        name="bitcoin-mainnet",
        native_symbol="BTC",
        provider_aliases=("bitcoin", "btc"),
        provider="configured_explorer" if bitcoin_configured else "unconfigured",
        supported_data_sources=("bitcoin_utxo_explorer",) if bitcoin_configured else (),
    ))
    additional = _additional_networks(env.get("NOEMA_ADDITIONAL_NETWORKS_JSON", ""))
    identities = {record.canonical_network_id for record in records}
    aliases = {alias.lower() for record in records for alias in record.provider_aliases}
    for record in additional:
        if record.canonical_network_id in identities:
            raise ValueError("additional network identity is duplicated")
        if aliases.intersection(alias.lower() for alias in record.provider_aliases):
            raise ValueError("additional network provider alias is duplicated")
        identities.add(record.canonical_network_id)
        aliases.update(alias.lower() for alias in record.provider_aliases)
        records.append(record)
    return tuple(records)


def resolve_provider_network(
    provider_id: str, environ: Mapping[str, str] | None = None,
) -> NetworkRecord | None:
    """Resolve a source network slug only through explicit registry aliases."""
    normalized = provider_id.strip().lower()
    if not normalized:
        return None
    return next((record for record in load_network_registry(environ)
                 if normalized in {alias.lower() for alias in record.provider_aliases}), None)


def _additional_networks(raw: str) -> tuple[NetworkRecord, ...]:
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("additional network metadata is invalid") from exc
    if not isinstance(values, list):
        raise TypeError("additional network metadata must be a list")
    result = []
    seen_ids: set[str] = set()
    seen_aliases: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            raise TypeError("additional network entry is invalid")
        allowed = {"canonical_network_id", "chain_family", "name", "native_symbol", "provider_aliases"}
        if set(item) - allowed:
            raise ValueError("additional network entry has unsupported fields")
        identity = item.get("canonical_network_id")
        family = item.get("chain_family")
        name = item.get("name")
        symbol = item.get("native_symbol")
        provider_aliases = item.get("provider_aliases", [])
        if not isinstance(identity, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._-]{1,128}", identity) is None:
            raise ValueError("additional network canonical identity is invalid")
        if family not in {"evm", "solana", "utxo", "other"}:
            raise ValueError("additional network family is invalid")
        if family == "evm" and re.fullmatch(r"eip155:[1-9][0-9]{0,19}", identity) is None:
            raise ValueError("additional EVM network must use a CAIP-2 eip155 identity")
        if family == "solana" and not identity.startswith("solana:"):
            raise ValueError("additional Solana network must use a CAIP-2 solana identity")
        if family == "utxo" and not identity.startswith("bip122:"):
            raise ValueError("additional UTXO network must use a CAIP-2 bip122 identity")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("additional network name is invalid")
        if symbol is not None and (not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)):
            raise ValueError("additional network native symbol is invalid")
        if not isinstance(provider_aliases, list) or not provider_aliases:
            raise ValueError("additional network provider aliases are required")
        normalized_aliases = []
        for alias in provider_aliases:
            if not isinstance(alias, str) or not _NAME.fullmatch(alias):
                raise ValueError("additional network provider alias is invalid")
            normalized = alias.lower()
            if normalized in seen_aliases:
                raise ValueError("additional network provider alias is duplicated")
            seen_aliases.add(normalized)
            normalized_aliases.append(normalized)
        if identity in seen_ids:
            raise ValueError("additional network identity is duplicated")
        seen_ids.add(identity)
        result.append(NetworkRecord(
            canonical_network_id=identity,
            chain_family=family,
            name=name,
            native_symbol=symbol,
            provider_aliases=tuple(normalized_aliases),
            provider="unconfigured",
        ))
    return tuple(result)


def load_evm_chains(environ: Mapping[str, str] | None = None) -> tuple[EVMChain, ...]:
    """Build configured EVM observation records, extending defaults by chain ID.

    `NOEMA_EVM_CHAIN_REGISTRY_JSON` accepts metadata only. RPC URLs must remain
    in per-chain environment variables (`NOEMA_EVM_RPC_URL_<chain_id>`); legacy
    name-based and global variables are fallback-compatible for existing users.
    """
    env = os.environ if environ is None else environ
    metadata = _metadata(env.get("NOEMA_EVM_CHAIN_REGISTRY_JSON", ""))
    chain_ids = set(_DEFAULTS) | set(metadata)
    for key in env:
        match = _RPC_ID_KEY.fullmatch(key)
        if match:
            chain_ids.add(int(match.group(1)))

    chains = []
    preferred_order = (1, 8453, 42161, 10, 137, 56, 43114)
    ordered_chain_ids = [chain_id for chain_id in preferred_order if chain_id in chain_ids]
    ordered_chain_ids.extend(sorted(chain_ids - set(preferred_order)))
    for chain_id in ordered_chain_ids:
        default = _DEFAULTS.get(chain_id)
        configured = metadata.get(chain_id, {})
        fallback_name, fallback_symbol, default_rpc, price_symbol = default or (
            f"eip155-{chain_id}", "NATIVE", "", None,
        )
        name = configured.get("name", fallback_name)
        symbol = configured.get("native_symbol", fallback_symbol)
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("EVM chain name is invalid")
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError("EVM native symbol is invalid")
        configured_price_symbol = configured.get("native_price_symbol", price_symbol)
        if configured_price_symbol is not None and (
            not isinstance(configured_price_symbol, str)
            or not _SYMBOL.fullmatch(configured_price_symbol)
        ):
            raise ValueError("EVM native price symbol is invalid")

        endpoint = env.get(f"NOEMA_EVM_RPC_URL_{chain_id}")
        if not endpoint and chain_id in _LEGACY_RPC_NAMES:
            endpoint = env.get(f"NOEMA_EVM_RPC_URL_{_LEGACY_RPC_NAMES[chain_id]}")
        # The legacy global endpoint historically represented the single watched
        # network (Ethereum). Never reuse it across unrelated chain IDs.
        global_rpc = env.get("NOEMA_EVM_RPC_URL") if chain_id == 1 else None
        endpoint = endpoint or global_rpc or default_rpc or None
        provider = (
            "configured_json_rpc" if endpoint and (
                env.get(f"NOEMA_EVM_RPC_URL_{chain_id}")
                or (chain_id in _LEGACY_RPC_NAMES
                    and env.get(f"NOEMA_EVM_RPC_URL_{_LEGACY_RPC_NAMES[chain_id]}"))
                or global_rpc
            ) else "default_public_json_rpc" if endpoint else "unconfigured"
        )
        chains.append(EVMChain(
            chain_id=chain_id,
            name=name,
            native_symbol=symbol,
            rpc_endpoint=endpoint,
            rpc_provider=provider,
            native_price_symbol=configured_price_symbol,
            supported_quote_sources=(
                ("0x_swap_v2_price",) if chain_id in _ZEROX_QUOTE_CHAIN_IDS else ()
            ),
        ))
    return tuple(chains)


def _metadata(raw: str) -> dict[int, dict[str, object]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("EVM chain registry metadata is invalid") from exc
    if not isinstance(value, list):
        raise TypeError("EVM chain registry metadata must be a list")
    result: dict[int, dict[str, object]] = {}
    for row in value:
        if not isinstance(row, dict):
            raise TypeError("EVM chain registry entry is invalid")
        chain_id = row.get("chain_id")
        if isinstance(chain_id, bool) or not isinstance(chain_id, int) or not 1 <= chain_id < 2**64:
            raise ValueError("EVM chain ID is invalid")
        if chain_id in result:
            raise ValueError("EVM chain ID is duplicated")
        allowed = {"chain_id", "name", "native_symbol", "native_price_symbol"}
        if set(row) - allowed:
            raise ValueError("EVM chain registry entry has unsupported fields")
        result[chain_id] = {key: value for key, value in row.items() if key != "chain_id"}
    return result
