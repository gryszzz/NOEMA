from __future__ import annotations

import subprocess

from noema import wallet_credentials


def test_presence_check_queries_keychain_metadata_without_password_output(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(wallet_credentials.sys, "platform", "darwin")
    monkeypatch.setattr(wallet_credentials.subprocess, "run", fake_run)

    assert wallet_credentials.solana_signing_credential_present() is True
    args, kwargs = calls[0]
    assert "find-generic-password" in args
    assert "-w" not in args
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_presence_check_fails_closed_when_keychain_is_unavailable(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("security", 2)

    monkeypatch.setattr(wallet_credentials.sys, "platform", "darwin")
    monkeypatch.setattr(wallet_credentials.subprocess, "run", fail)
    assert wallet_credentials.solana_signing_credential_present() is False


def test_presence_check_is_disabled_off_macos(monkeypatch) -> None:
    monkeypatch.setattr(wallet_credentials.sys, "platform", "linux")
    assert wallet_credentials.solana_signing_credential_present() is False


def test_venue_presence_checks_do_not_read_keychain_secret_data(monkeypatch) -> None:
    monkeypatch.setattr(wallet_credentials.sys, "platform", "darwin")
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)
    monkeypatch.setattr(wallet_credentials, "_credential_present", lambda *_args: True)

    def reject_secret_read(*_args, **_kwargs):
        raise AssertionError("presence check attempted to load secret bytes")

    monkeypatch.setattr(wallet_credentials, "_load_keychain_item", reject_secret_read)
    assert wallet_credentials.kalshi_key_id_present() is True
    assert wallet_credentials.polymarket_us_credentials_present() == (True, True)


def test_hosted_environment_credentials_work_on_linux_and_take_precedence(monkeypatch) -> None:
    monkeypatch.setattr(wallet_credentials.sys, "platform", "linux")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "hosted-kalshi-id")
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "hosted-polymarket-id")
    monkeypatch.setenv("POLYMARKET_US_SECRET_KEY", "hosted-polymarket-secret")

    assert wallet_credentials.kalshi_key_id_present()
    assert wallet_credentials.load_kalshi_key_id_in_api_boundary() == "hosted-kalshi-id"
    assert wallet_credentials.polymarket_us_credentials_present() == (True, True)
    assert wallet_credentials.load_polymarket_us_credentials_in_api_boundary() == (
        "hosted-polymarket-id", "hosted-polymarket-secret",
    )
    assert wallet_credentials.credential_source("POLYMARKET_US_SECRET_KEY") == "environment"
    resolved = wallet_credentials.ResolvedCredential("do-not-print", "environment")
    assert "do-not-print" not in repr(resolved)


def test_hosted_presence_reports_missing_polymarket_half_without_exposing_values(monkeypatch) -> None:
    monkeypatch.setattr(wallet_credentials.sys, "platform", "linux")
    monkeypatch.setenv("POLYMARKET_US_KEY_ID", "configured-id")
    monkeypatch.delenv("POLYMARKET_US_SECRET_KEY", raising=False)

    assert wallet_credentials.polymarket_us_credentials_present() == (True, False)
    try:
        wallet_credentials.load_polymarket_us_credentials_in_api_boundary()
    except RuntimeError as error:
        assert "configured-id" not in str(error)
    else:
        raise AssertionError("incomplete credentials must fail closed")
