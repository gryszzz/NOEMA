from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"


def _enabled(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TrenchCollectorConfig:
    enabled: bool = False
    jupiter_api_key: str | None = field(default=None, repr=False)
    solana_rpc_url: str = field(default=DEFAULT_SOLANA_RPC_URL, repr=False)
    solana_rpc_fallback_url: str | None = field(default=None, repr=False)
    due_limit: int = 12
    enrichment_limit: int = 1
    request_pause_seconds: float = 2.1

    @classmethod
    def from_env(cls) -> TrenchCollectorConfig:
        primary_rpc = os.getenv("NOEMA_SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL).strip()
        configured_fallback = (os.getenv("NOEMA_SOLANA_RPC_FALLBACK_URL") or "").strip()
        return cls(
            enabled=_enabled("NOEMA_TRENCH_ENABLED"),
            jupiter_api_key=os.getenv("NOEMA_JUPITER_API_KEY") or None,
            solana_rpc_url=primary_rpc,
            solana_rpc_fallback_url=configured_fallback or None,
            due_limit=int(os.getenv("NOEMA_TRENCH_DUE_LIMIT", "12")),
            enrichment_limit=int(os.getenv("NOEMA_TRENCH_ENRICHMENT_LIMIT", "1")),
            request_pause_seconds=float(
                os.getenv("NOEMA_TRENCH_REQUEST_PAUSE_SECONDS", "2.1")
            ),
        )

    def validate(self) -> None:
        if self.due_limit <= 0 or self.due_limit > 100:
            raise ValueError("NOEMA_TRENCH_DUE_LIMIT must be in [1, 100]")
        if self.enrichment_limit < 0 or self.enrichment_limit > self.due_limit:
            raise ValueError("invalid NOEMA_TRENCH_ENRICHMENT_LIMIT")
        if self.request_pause_seconds < 0:
            raise ValueError("NOEMA_TRENCH_REQUEST_PAUSE_SECONDS must be non-negative")
        if not self.solana_rpc_url:
            raise ValueError("NOEMA_SOLANA_RPC_URL cannot be empty")
