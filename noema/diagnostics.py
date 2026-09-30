from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .config import KalshiConfig
from .wallet_credentials import KALSHI_KEY_ID_KEYCHAIN_SERVICE, credential_source


@dataclass(frozen=True)
class ConfigDiagnostic:
    environment: str
    api_key_id_present: bool
    private_key_path_present: bool
    private_key_file_exists: bool
    api_key_id_provider: str
    private_key_provider: str
    live_orders_armed: bool
    master_halt: bool
    demo_ready: bool


def diagnose(config: KalshiConfig | None = None) -> ConfigDiagnostic:
    config = config or KalshiConfig.from_env()
    path_present = bool(
        config.private_key_path or config.private_key_pem or config.private_key_pem_b64
    )
    file_exists = bool(config.private_key_pem is not None or (
        config.private_key_pem_b64 is not None or (
            config.private_key_path and Path(config.private_key_path).is_file()
        )
    ))
    return ConfigDiagnostic(
        environment=config.environment,
        api_key_id_present=bool(config.key_id),
        private_key_path_present=path_present,
        private_key_file_exists=file_exists,
        api_key_id_provider=credential_source(
            "KALSHI_API_KEY_ID", keychain_service=KALSHI_KEY_ID_KEYCHAIN_SERVICE,
        ),
        private_key_provider=config.private_key_source,
        live_orders_armed=config.allow_live_orders,
        master_halt=config.master_halt,
        demo_ready=(
            config.environment == "demo"
            and bool(config.key_id)
            and file_exists
        ),
    )


def diagnostic_dict(config: KalshiConfig | None = None) -> dict[str, object]:
    return asdict(diagnose(config))
