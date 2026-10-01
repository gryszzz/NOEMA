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
    137: ("polygon", "POL", "https://polygon-bor-rpc.publicnode.com", "POL"),
}
_LEGACY_RPC_NAMES = {1: "ETHEREUM", 8453: "BASE", 137: "POLYGON"}
_ZEROX_QUOTE_CHAIN_IDS = {1, 8453, 137}


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
    preferred_order = (1, 8453, 137)
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

        endpoint = (
            env.get(f"NOEMA_EVM_RPC_URL_{chain_id}")
            or env.get(f"NOEMA_EVM_RPC_URL_{_LEGACY_RPC_NAMES[chain_id]}")
            if chain_id in _LEGACY_RPC_NAMES else env.get(f"NOEMA_EVM_RPC_URL_{chain_id}")
        )
        endpoint = endpoint or env.get("NOEMA_EVM_RPC_URL") or default_rpc or None
        provider = "configured_json_rpc" if endpoint else "unconfigured"
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
