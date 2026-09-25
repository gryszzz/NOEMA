from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class KalshiConfig:
    environment: str = "demo"
    key_id: str | None = None
    private_key_path: str | None = None
    allow_live_orders: bool = False

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
        private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        allow_live = os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") == "1"

        if private_key_path and not Path(private_key_path).exists():
            raise FileNotFoundError(private_key_path)

        return cls(
            environment=environment,
            key_id=key_id,
            private_key_path=private_key_path,
            allow_live_orders=allow_live,
        )
