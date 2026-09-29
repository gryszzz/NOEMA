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
