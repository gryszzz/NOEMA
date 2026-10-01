from __future__ import annotations

import asyncio
import json
from contextlib import redirect_stdout
from io import StringIO

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
    output = StringIO()
    with redirect_stdout(output):
        state = asyncio.run(_kalshi_state())

    diagnostic = json.loads(output.getvalue().strip())
    assert state.status == "unconfigured"
    assert diagnostic["event"] == "agent_kalshi_credential_diagnostic"
    assert {key: value for key, value in diagnostic.items() if key.startswith("kalshi_")} == {
        "kalshi_api_key_id_present": "yes",
        "kalshi_api_key_id_provider": "environment",
        "kalshi_private_key_configured": "yes",
        "kalshi_private_key_provider": "environment_path",
        "kalshi_private_key_file_status": "readable",
        "kalshi_environment": "production",
    }
    assert key_id not in output.getvalue()
    assert pem_contents not in output.getvalue()
