"""Credential boundary for hosted environment secrets and local macOS Keychain."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SOLANA_SIGNER_KEYCHAIN_SERVICE = "com.noema.solana.owner-wallet"
SOLANA_SIGNER_KEYCHAIN_ACCOUNT = "noema-owner"
EVM_SIGNER_KEYCHAIN_SERVICE = "com.noema.evm.owner-wallet"
EVM_SIGNER_KEYCHAIN_ACCOUNT = "noema-owner"
BITCOIN_SIGNER_KEYCHAIN_SERVICE = "com.noema.bitcoin.owner-wallet"
BITCOIN_SIGNER_KEYCHAIN_ACCOUNT = "noema-owner"
KALSHI_KEY_ID_KEYCHAIN_SERVICE = "com.noema.kalshi.key-id"
KALSHI_KEY_ID_KEYCHAIN_ACCOUNT = "noema-owner"
POLYMARKET_US_KEY_ID_KEYCHAIN_SERVICE = "com.noema.polymarket-us.key-id"
POLYMARKET_US_SECRET_KEY_KEYCHAIN_SERVICE = "com.noema.polymarket-us.secret-key"
POLYMARKET_US_KEYCHAIN_ACCOUNT = "noema-owner"
RENDER_KALSHI_SECRET_FILE = Path("/etc/secrets/kalshi.pem")


@dataclass(frozen=True)
class ResolvedCredential:
    """A secret and its safe provider label; repr intentionally omits the value."""

    value: str | bytes = field(repr=False)
    provider: str


def _environment_credential(name: str) -> ResolvedCredential | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return ResolvedCredential(value=value.strip(), provider="environment")


def private_key_file_status(path: str | Path | None) -> str:
    """Check a PEM path without reading or returning any key material."""
    if not path:
        return "not_configured"
    try:
        with Path(path).open("rb"):
            return "readable"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def _resolve_secret(
    env_name: str, *, keychain_service: str | None = None,
    keychain_account: str = "noema-owner", label: str,
) -> ResolvedCredential | None:
    """Resolve environment first, then the explicitly supported local Keychain."""
    configured = _environment_credential(env_name)
    if configured is not None:
        return configured
    if keychain_service is None or sys.platform != "darwin":
        return None
    if not _credential_present(keychain_service, keychain_account):
        return None
    try:
        value = _load_keychain_item(keychain_service, keychain_account, label=label)
    except RuntimeError:
        return None
    return ResolvedCredential(value=value, provider="macos_keychain")


def credential_source(env_name: str, *, keychain_service: str | None = None,
                      keychain_account: str = "noema-owner") -> str:
    """Return a safe provider label without returning secret material."""
    if _environment_credential(env_name) is not None:
        return "environment"
    if (keychain_service and sys.platform == "darwin"
            and _credential_present(keychain_service, keychain_account)):
        return "macos_keychain"
    return "unavailable"


def resolve_kalshi_private_key() -> tuple[str | None, str | bytes | None, str]:
    """Resolve a mounted PEM path first, with environment PEM as a fallback.

    Render Secret Files are mounted at ``/etc/secrets/<filename>``. Keep PEM
    bytes out of the application config; the signer reads the selected path
    only when it needs to sign an authenticated request.
    """
    configured_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if configured_path and configured_path.strip():
        path = Path(configured_path.strip()).expanduser()
        provider = (
            "render_secret_file"
            if os.getenv("RENDER", "").strip().lower() == "true"
            and path.is_relative_to(Path("/etc/secrets"))
            else "environment_path"
        )
        # Retain an explicitly configured missing path so diagnostics can say
        # MISSING instead of silently selecting another credential source.
        return str(path), None, provider

    if os.getenv("RENDER", "").strip().lower() == "true":
        status = private_key_file_status(RENDER_KALSHI_SECRET_FILE)
        if status != "missing":
            return str(RENDER_KALSHI_SECRET_FILE), None, "render_secret_file"

    # Retain compatibility for local/dev and existing hosted configurations
    # that supply a base64 PEM in the environment.
    encoded = _environment_credential("KALSHI_PRIVATE_KEY_PEM_B64")
    if encoded is not None:
        return None, str(encoded.value), "environment"

    default_path = Path.home() / ".config/noema/credentials/kalshi.pem"
    if private_key_file_status(default_path) == "missing":
        return None, None, "unavailable"
    # Keep local private-key bytes out of config and diagnostics. The signer
    # boundary reads the configured file only when it must construct a signer.
    return str(default_path), None, "local_file"


def solana_signing_credential_present() -> bool:
    """Check item metadata only; never request or return its password data."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                SOLANA_SIGNER_KEYCHAIN_SERVICE,
                "-a",
                SOLANA_SIGNER_KEYCHAIN_ACCOUNT,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def evm_signing_credential_present() -> bool:
    return _credential_present(EVM_SIGNER_KEYCHAIN_SERVICE, EVM_SIGNER_KEYCHAIN_ACCOUNT)


def bitcoin_signing_credential_present() -> bool:
    return _credential_present(BITCOIN_SIGNER_KEYCHAIN_SERVICE, BITCOIN_SIGNER_KEYCHAIN_ACCOUNT)


def kalshi_key_id_present() -> bool:
    return credential_source(
        "KALSHI_API_KEY_ID", keychain_service=KALSHI_KEY_ID_KEYCHAIN_SERVICE,
        keychain_account=KALSHI_KEY_ID_KEYCHAIN_ACCOUNT,
    ) != "unavailable"


def polymarket_us_credentials_present() -> tuple[bool, bool]:
    """Return credential presence without reading Keychain secret material."""
    return (
        credential_source(
            "POLYMARKET_US_KEY_ID", keychain_service=POLYMARKET_US_KEY_ID_KEYCHAIN_SERVICE,
            keychain_account=POLYMARKET_US_KEYCHAIN_ACCOUNT,
        ) != "unavailable",
        credential_source(
            "POLYMARKET_US_SECRET_KEY", keychain_service=POLYMARKET_US_SECRET_KEY_KEYCHAIN_SERVICE,
            keychain_account=POLYMARKET_US_KEYCHAIN_ACCOUNT,
        ) != "unavailable",
    )


def polymarket_us_credential_sources() -> dict[str, str]:
    """Safe provider labels for each authenticated account credential."""
    return {
        "key_id": credential_source(
            "POLYMARKET_US_KEY_ID", keychain_service=POLYMARKET_US_KEY_ID_KEYCHAIN_SERVICE,
            keychain_account=POLYMARKET_US_KEYCHAIN_ACCOUNT,
        ),
        "secret_key": credential_source(
            "POLYMARKET_US_SECRET_KEY", keychain_service=POLYMARKET_US_SECRET_KEY_KEYCHAIN_SERVICE,
            keychain_account=POLYMARKET_US_KEYCHAIN_ACCOUNT,
        ),
    }


def _load_keychain_item(service: str, account: str, *, label: str) -> str:
    """Retrieve a credential in memory for one API/signing boundary only."""
    if sys.platform != "darwin":
        raise RuntimeError(f"{label} Keychain access is supported only on macOS")
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"{label} credential access failed") from None
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"{label} credential access failed")
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeError(f"{label} credential is malformed") from None


def load_kalshi_key_id_in_api_boundary() -> str:
    credential = _resolve_secret(
        "KALSHI_API_KEY_ID", keychain_service=KALSHI_KEY_ID_KEYCHAIN_SERVICE,
        keychain_account=KALSHI_KEY_ID_KEYCHAIN_ACCOUNT, label="Kalshi API",
    )
    if credential is None:
        raise RuntimeError("Kalshi API key ID is unavailable")
    return str(credential.value)


def load_polymarket_us_credentials_in_api_boundary() -> tuple[str, str]:
    """Load credentials only for authenticated Polymarket US requests."""
    key_id = _resolve_secret(
        "POLYMARKET_US_KEY_ID", keychain_service=POLYMARKET_US_KEY_ID_KEYCHAIN_SERVICE,
        keychain_account=POLYMARKET_US_KEYCHAIN_ACCOUNT, label="Polymarket US API",
    )
    secret = _resolve_secret(
        "POLYMARKET_US_SECRET_KEY", keychain_service=POLYMARKET_US_SECRET_KEY_KEYCHAIN_SERVICE,
        keychain_account=POLYMARKET_US_KEYCHAIN_ACCOUNT, label="Polymarket US API",
    )
    if key_id is None or secret is None:
        raise RuntimeError("Polymarket US account credentials are unavailable")
    return str(key_id.value), str(secret.value)


def _credential_present(service: str, account: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def load_solana_keypair_in_signer_boundary():
    """Load and parse the owner key only in the isolated signer process.

    Never call this from cognition, the dashboard, or the NOEMA parent runtime.
    The `security -w` output is captured privately here and is never included in
    an exception, return value, or log message.
    """
    if sys.platform != "darwin":
        raise RuntimeError("local Solana signer is supported only on macOS")

    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                SOLANA_SIGNER_KEYCHAIN_SERVICE,
                "-a",
                SOLANA_SIGNER_KEYCHAIN_ACCOUNT,
                "-w",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("signer credential access failed") from None

    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("signer credential access failed")

    # Parsing happens inside this module's isolated signer process. Do not
    # interpolate `result.stdout` or parse errors into messages.
    try:
        from solders.keypair import Keypair

        raw = result.stdout.strip()
        if raw.startswith(b"["):
            import json

            keypair = Keypair.from_bytes(bytes(json.loads(raw)))
        else:
            keypair = Keypair.from_base58_string(raw.decode("ascii"))
    except Exception:  # noqa: BLE001 - suppress parser detail at the signer boundary
        raise RuntimeError("signer credential is not a valid Solana keypair") from None
    return keypair


def load_evm_account_in_signer_boundary():
    """Load the EVM key only inside the isolated signer process."""
    if sys.platform != "darwin":
        raise RuntimeError("local EVM signer is supported only on macOS")
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                EVM_SIGNER_KEYCHAIN_SERVICE,
                "-a",
                EVM_SIGNER_KEYCHAIN_ACCOUNT,
                "-w",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("EVM signer credential access failed") from None
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("EVM signer credential access failed")
    try:
        from eth_account import Account

        credential = result.stdout.strip()
        if len(credential) == 32:
            private_key = credential
        else:
            encoded = credential.decode("ascii")
            encoded = encoded.removeprefix("0x")
            if len(encoded) != 64:
                raise ValueError
            private_key = bytes.fromhex(encoded)
        account = Account.from_key(private_key)
    except Exception:  # noqa: BLE001 - credential parser errors must stay secret-safe
        raise RuntimeError("EVM signer credential is not a valid private key") from None
    return account


def load_bitcoin_private_key_in_signer_boundary():
    """Load only the Bitcoin identity's credential inside the signer process."""
    if sys.platform != "darwin":
        raise RuntimeError("local Bitcoin signer is supported only on macOS")
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                BITCOIN_SIGNER_KEYCHAIN_SERVICE,
                "-a",
                BITCOIN_SIGNER_KEYCHAIN_ACCOUNT,
                "-w",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Bitcoin signer credential access failed") from None
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("Bitcoin signer credential access failed")
    try:
        from embit import ec, networks

        raw = result.stdout.strip()
        try:
            key = ec.PrivateKey.from_wif(raw.decode("ascii"))
        except Exception:  # noqa: BLE001 - try the supported raw-hex representation
            encoded = raw.decode("ascii")
            encoded = encoded.removeprefix("0x")
            if len(encoded) != 64:
                raise ValueError
            key = ec.PrivateKey(bytes.fromhex(encoded), network=networks.NETWORKS["main"])
        if key.network["name"] != "Mainnet":
            raise ValueError
    except Exception:  # noqa: BLE001 - suppress private-key parser detail
        raise RuntimeError("Bitcoin signer credential is not a supported mainnet key") from None
    return key
