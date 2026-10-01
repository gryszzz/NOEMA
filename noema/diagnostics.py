from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import KalshiConfig
from .wallet_credentials import (
    KALSHI_KEY_ID_KEYCHAIN_SERVICE,
    credential_source,
    private_key_file_status,
)


@dataclass(frozen=True)
class ConfigDiagnostic:
    environment: str
    api_key_id_present: bool
    private_key_path_present: bool
    private_key_file_exists: bool
    private_key_file_status: str
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
    if config.private_key_pem is not None or config.private_key_pem_b64 is not None:
        file_status = "environment"
    else:
        file_status = private_key_file_status(config.private_key_path)
    file_exists = file_status in {"readable", "unreadable"}
    return ConfigDiagnostic(
        environment=config.environment,
        api_key_id_present=bool(config.key_id),
        private_key_path_present=path_present,
        private_key_file_exists=file_exists,
        private_key_file_status=file_status,
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


def kalshi_runtime_credential_diagnostic(
    config: KalshiConfig | None = None,
) -> dict[str, str]:
    """Return only safe credential presence and source metadata for runtime logs."""
    result = diagnose(config)
    file_status = (
        "missing" if result.private_key_file_status == "not_configured"
        else result.private_key_file_status
    )
    return {
        "kalshi_api_key_id_present": "yes" if result.api_key_id_present else "no",
        "kalshi_api_key_id_header_safe": (
            "yes" if _kalshi_key_id_header_safe(config.key_id) else "no"
        ),
        "kalshi_api_key_id_provider": result.api_key_id_provider,
        "kalshi_private_key_configured": "yes" if result.private_key_path_present else "no",
        "kalshi_private_key_provider": result.private_key_provider,
        "kalshi_private_key_file_status": file_status,
        "kalshi_environment": result.environment,
    }


def _kalshi_key_id_header_safe(value: str | None) -> bool:
    """Validate only safe HTTP header character constraints, never disclose value."""
    return bool(
        value
        and value.isascii()
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )
