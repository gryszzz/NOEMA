"""Presence-only access to NOEMA's dedicated macOS Keychain credential slot."""

from __future__ import annotations

import subprocess
import sys

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
    return _credential_present(KALSHI_KEY_ID_KEYCHAIN_SERVICE, KALSHI_KEY_ID_KEYCHAIN_ACCOUNT)


def polymarket_us_credentials_present() -> tuple[bool, bool]:
    """Return Key ID and secret-key presence without retrieving either value."""
    return (
        _credential_present(POLYMARKET_US_KEY_ID_KEYCHAIN_SERVICE, POLYMARKET_US_KEYCHAIN_ACCOUNT),
        _credential_present(
            POLYMARKET_US_SECRET_KEY_KEYCHAIN_SERVICE,
            POLYMARKET_US_KEYCHAIN_ACCOUNT,
        ),
    )


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
    return _load_keychain_item(
        KALSHI_KEY_ID_KEYCHAIN_SERVICE,
        KALSHI_KEY_ID_KEYCHAIN_ACCOUNT,
        label="Kalshi API",
    )


def load_polymarket_us_credentials_in_api_boundary() -> tuple[str, str]:
    """Load credentials only for authenticated Polymarket US requests."""
    return (
        _load_keychain_item(
            POLYMARKET_US_KEY_ID_KEYCHAIN_SERVICE,
            POLYMARKET_US_KEYCHAIN_ACCOUNT,
            label="Polymarket US API",
        ),
        _load_keychain_item(
            POLYMARKET_US_SECRET_KEY_KEYCHAIN_SERVICE,
            POLYMARKET_US_KEYCHAIN_ACCOUNT,
            label="Polymarket US API",
        ),
    )


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
