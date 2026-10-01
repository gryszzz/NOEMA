from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noema.config import KalshiConfig, kalshi_credential_presence
from noema.diagnostics import diagnose
from noema.venues.kalshi import KalshiSigner
from noema.wallet_credentials import private_key_file_status


def _pem_bytes() -> bytes:
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _clear_kalshi_env(monkeypatch) -> None:
    for name in (
        "KALSHI_PRIVATE_KEY_PATH",
        "KALSHI_PRIVATE_KEY_PEM_B64",
        "KALSHI_API_KEY_ID",
        "NOEMA_ALLOW_LIVE_ORDERS",
        "NOEMA_MASTER_HALT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_render_secret_file_is_selected_and_signer_reads_it(monkeypatch, tmp_path) -> None:
    from noema import wallet_credentials

    _clear_kalshi_env(monkeypatch)
    pem_path = tmp_path / "kalshi.pem"
    pem_path.write_bytes(_pem_bytes())
    # Render-managed files need not inherit local owner-only file permissions.
    pem_path.chmod(0o644)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "fixture-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM_B64", "invalid-fallback-is-not-selected")
    monkeypatch.setattr(wallet_credentials, "RENDER_KALSHI_SECRET_FILE", pem_path)

    config = KalshiConfig.from_env()
    assert config.private_key_path == str(pem_path)
    assert config.private_key_source == "render_secret_file"
    assert config.private_key_pem_b64 is None
    assert KalshiSigner(config.key_id, config.private_key_path).headers(
        "GET", "/trade-api/v2/portfolio/balance",
    )["KALSHI-ACCESS-SIGNATURE"]

    diagnostic = diagnose(config)
    assert diagnostic.private_key_file_status == "readable"
    assert diagnostic.private_key_file_exists is True
    assert diagnostic.live_orders_armed is False
    assert "fixture-key-id" not in repr(config)
    assert pem_path.read_bytes().decode("ascii") not in repr(config)


def test_explicit_missing_secret_file_path_is_diagnosed_without_fallback(monkeypatch, tmp_path) -> None:
    _clear_kalshi_env(monkeypatch)
    missing_path = tmp_path / "missing-kalshi.pem"
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(missing_path))
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PEM_B64", base64.b64encode(b"fallback").decode())
    monkeypatch.setenv("KALSHI_API_KEY_ID", "fixture-key-id")

    config = KalshiConfig.from_env()
    diagnostic = diagnose(config)
    assert config.private_key_path == str(missing_path)
    assert diagnostic.private_key_path_present is True
    assert diagnostic.private_key_file_exists is False
    assert diagnostic.private_key_file_status == "missing"
    assert kalshi_credential_presence() == (True, True, False)
    assert str(missing_path) not in repr(diagnostic)


def test_unreadable_secret_file_is_distinguished_without_reading_contents(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "kalshi.pem"
    path.write_bytes(_pem_bytes())
    original_open = type(path).open

    def deny_open(self, *args, **kwargs):
        if self == path:
            raise PermissionError("read denied")
        return original_open(self, *args, **kwargs)

    from noema import wallet_credentials

    _clear_kalshi_env(monkeypatch)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "fixture-key-id")
    monkeypatch.setattr(wallet_credentials, "RENDER_KALSHI_SECRET_FILE", path)
    monkeypatch.setattr(type(path), "open", deny_open)

    config = KalshiConfig.from_env()
    diagnostic = diagnose(config)
    assert private_key_file_status(path) == "unreadable"
    assert diagnostic.private_key_file_status == "unreadable"
    assert diagnostic.private_key_file_exists is True
    assert kalshi_credential_presence() == (True, True, False)
