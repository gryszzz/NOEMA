from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .wallet_credentials import kalshi_key_id_present, load_kalshi_key_id_in_api_boundary


@dataclass(frozen=True)
class KalshiConfig:
    environment: str = "demo"
    key_id: str | None = None
    private_key_path: str | None = None
    allow_live_orders: bool = False
    master_halt: bool = False

    @property
    def base_url(self) -> str:
        if self.environment == "production":
            return "https://external-api.kalshi.com/trade-api/v2"
        return "https://external-api.demo.kalshi.co/trade-api/v2"

    @classmethod
    def from_env(cls) -> KalshiConfig:
        environment = os.getenv("NOEMA_KALSHI_ENV", "demo").strip().lower()
        if environment not in {"demo", "production"}:
            raise ValueError("NOEMA_KALSHI_ENV must be demo or production")

        key_id = os.getenv("KALSHI_API_KEY_ID")
        if not key_id and kalshi_key_id_present():
            key_id = load_kalshi_key_id_in_api_boundary()
        configured_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        default_path = Path.home() / ".config/noema/credentials/kalshi.pem"
        private_key_path = configured_path or (str(default_path) if default_path.is_file() else None)
        allow_live = os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") == "1"
        master_halt = os.getenv("NOEMA_MASTER_HALT", "0") == "1"

        if private_key_path and not Path(private_key_path).exists():
            raise FileNotFoundError(private_key_path)

        return cls(
            environment=environment,
            key_id=key_id,
            private_key_path=private_key_path,
            allow_live_orders=allow_live,
            master_halt=master_halt,
        )


def kalshi_production_read_only_config() -> KalshiConfig:
    """Production market/account data without production-order authority."""
    configured = KalshiConfig.from_env()
    return KalshiConfig(
        environment="production",
        key_id=configured.key_id,
        private_key_path=configured.private_key_path,
        allow_live_orders=False,
        master_halt=True,
    )
