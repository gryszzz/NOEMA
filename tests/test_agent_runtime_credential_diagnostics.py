from __future__ import annotations

import asyncio

from noema import wallet_credentials
from noema.agent_runtime import _kalshi_state


def test_unconfigured_worker_cycle_reports_safe_kalshi_credential_metadata(
    monkeypatch, tmp_path,
) -> None:
    key_id = "fixture-key-id-must-not-be-logged"
    pem_contents = "fixture-private-key-must-not-be-logged"
    pem_path = tmp_path / "kalshi.pem"
    pem_path.write_text(pem_contents)

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("KALSHI_API_KEY_ID", key_id)
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(pem_path))
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PEM_B64", raising=False)
    monkeypatch.setattr(wallet_credentials, "RENDER_KALSHI_SECRET_FILE", pem_path)

    def fail_telemetry(_config):
        raise RuntimeError("fixture connection failure")

    monkeypatch.setattr("noema.agent_runtime.KalshiTelemetry", fail_telemetry)
    state = asyncio.run(_kalshi_state())

    assert state.status == "unconfigured"
    assert "key_id=present" in state.detail
    assert "private_key=readable" in state.detail
    assert "provider=environment_path" in state.detail
    assert key_id not in state.detail
    assert pem_contents not in state.detail
