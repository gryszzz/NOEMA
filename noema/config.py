from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .wallet_credentials import (
    kalshi_key_id_present,
    load_kalshi_key_id_in_api_boundary,
    resolve_kalshi_private_key,
)


def resolve_kalshi_private_key_path() -> str | None:
    path, _pem, _provider = resolve_kalshi_private_key()
    return path


def kalshi_credential_presence() -> tuple[bool, bool, bool]:
    """Return key ID/PEM presence without retrieving or exposing credentials."""
    key_id_present = bool(os.getenv("KALSHI_API_KEY_ID")) or kalshi_key_id_present()
    pem_path, pem, _provider = resolve_kalshi_private_key()
    protected = bool(
        pem is not None and pem_path is None
        or pem_path and Path(pem_path).is_file()
        and (Path(pem_path).stat().st_mode & 0o077) == 0
    )
    return key_id_present, bool(pem_path or pem), protected


@dataclass(frozen=True)
class KalshiConfig:
    environment: str = "demo"
    key_id: str | None = field(default=None, repr=False, compare=False)
    private_key_path: str | None = None
    private_key_pem: bytes | None = field(default=None, repr=False, compare=False)
    private_key_pem_b64: str | None = field(default=None, repr=False, compare=False)
    private_key_source: str = "unavailable"
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
            try:
                key_id = load_kalshi_key_id_in_api_boundary()
            except RuntimeError:
                # Metadata can say an item exists while Keychain access is
                # unavailable (for example during a locked session). Keep
                # public market reads healthy and private account access closed.
                key_id = None
        private_key_path, private_key_material, private_key_source = resolve_kalshi_private_key()
        private_key_pem = (private_key_material
                           if isinstance(private_key_material, bytes) else None)
        private_key_pem_b64 = (private_key_material
                               if isinstance(private_key_material, str) else None)
        allow_live = os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") == "1"
        master_halt = os.getenv("NOEMA_MASTER_HALT", "0") == "1"

        if private_key_path and not Path(private_key_path).is_file():
            raise FileNotFoundError(private_key_path)

        return cls(
            environment=environment,
            key_id=key_id,
            private_key_path=private_key_path,
            private_key_pem=private_key_pem,
            private_key_pem_b64=private_key_pem_b64,
            private_key_source=private_key_source,
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
        private_key_pem=configured.private_key_pem,
        private_key_pem_b64=configured.private_key_pem_b64,
        private_key_source=configured.private_key_source,
        allow_live_orders=False,
        master_halt=True,
    )
