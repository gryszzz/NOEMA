from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Chain(str, Enum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    BASE = "base"
    POLYGON = "polygon"
    BITCOIN = "bitcoin"


class WalletRole(str, Enum):
    TREASURY = "treasury"
    AGENT = "agent"
    WATCH_ONLY = "watch_only"


class WalletProvider(str, Enum):
    PHANTOM_EXTERNAL = "phantom_external"
    PRIVY_SERVER = "privy_server"
    TURNKEY_SERVER = "turnkey_server"
    LOCAL_DEV = "local_dev"


@dataclass(frozen=True)
class WalletDescriptor:
    wallet_id: str
    label: str
    provider: WalletProvider
    role: WalletRole
    chains: frozenset[Chain]
    addresses: dict[Chain, str]
    enabled: bool = True
