from __future__ import annotations

import asyncio
import json
from contextlib import redirect_stdout
from io import StringIO

from noema import wallet_credentials
from noema.agent_runtime import _kalshi_state
from noema.venues.kalshi import KalshiCredentialError


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
        raise KalshiCredentialError("private_key_malformed")

    monkeypatch.setattr("noema.agent_runtime.KalshiTelemetry", fail_telemetry)
    output = StringIO()
    with redirect_stdout(output):
        state = asyncio.run(_kalshi_state())

    diagnostic = json.loads(output.getvalue().splitlines()[0])
    assert state.status == "degraded"
    assert state.detail == "private key malformed or incompatible"
    assert diagnostic["event"] == "agent_kalshi_credential_diagnostic"
    assert {key: value for key, value in diagnostic.items() if key.startswith("kalshi_")} == {
        "kalshi_api_key_id_present": "yes",
        "kalshi_api_key_id_header_safe": "yes",
        "kalshi_api_key_id_provider": "environment",
        "kalshi_private_key_configured": "yes",
        "kalshi_private_key_provider": "environment_path",
        "kalshi_private_key_file_status": "readable",
        "kalshi_environment": "production",
    }
    assert key_id not in output.getvalue()
    assert pem_contents not in output.getvalue()


def test_kalshi_authenticated_rejection_is_distinguished_without_request_details(monkeypatch):
    import httpx

    request = httpx.Request("GET", "https://example.invalid/private", headers={
        "authorization": "do-not-log-this",
    })
    response = httpx.Response(401, request=request, text="do-not-log-response")

    class RejectingTelemetry:
        def __init__(self, _config):
            pass

        async def orders(self):
            raise httpx.HTTPStatusError("do-not-log-error", request=request, response=response)

        async def fills(self):
            return []

        async def positions(self):
            return []

        async def close(self):
            pass

    monkeypatch.setattr("noema.agent_runtime.kalshi_production_read_only_config", lambda: object())
    monkeypatch.setattr("noema.agent_runtime.kalshi_runtime_credential_diagnostic", lambda _c: {})
    monkeypatch.setattr("noema.agent_runtime.KalshiTelemetry", RejectingTelemetry)
    output = StringIO()
    with redirect_stdout(output):
        state = asyncio.run(_kalshi_state())

    assert state.status == "degraded"
    assert state.detail == "authenticated API rejected credentials (HTTP 401)"
    assert '"result": "authenticated_api_rejection"' in output.getvalue()
    assert "do-not-log" not in output.getvalue()
    assert "authorization" not in output.getvalue()


def test_kalshi_network_failure_is_distinguished_without_url_or_exception_text(monkeypatch):
    import httpx

    request = httpx.Request("GET", "https://example.invalid/private?secret=do-not-log")

    class OfflineTelemetry:
        def __init__(self, _config):
            pass

        async def orders(self):
            raise httpx.ConnectError("do-not-log-network-detail", request=request)

        async def fills(self):
            return []

        async def positions(self):
            return []

        async def close(self):
            pass

    monkeypatch.setattr("noema.agent_runtime.kalshi_production_read_only_config", lambda: object())
    monkeypatch.setattr("noema.agent_runtime.kalshi_runtime_credential_diagnostic", lambda _c: {})
    monkeypatch.setattr("noema.agent_runtime.KalshiTelemetry", OfflineTelemetry)
    output = StringIO()
    with redirect_stdout(output):
        state = asyncio.run(_kalshi_state())

    assert state.status == "degraded"
    assert state.detail == "Kalshi network request failed (ConnectError)"
    assert '"result": "network_failure"' in output.getvalue()
    assert "do-not-log" not in output.getvalue()
    assert "example.invalid" not in output.getvalue()


def test_kalshi_local_protocol_failure_is_not_reported_as_network_failure(monkeypatch):
    import httpx

    request = httpx.Request("GET", "https://example.invalid/private")

    class InvalidRequestTelemetry:
        def __init__(self, _config):
            pass

        async def orders(self):
            raise httpx.LocalProtocolError("do-not-log-header-value", request=request)

        async def fills(self):
            return []

        async def positions(self):
            return []

        async def close(self):
            pass

    monkeypatch.setattr("noema.agent_runtime.kalshi_production_read_only_config", lambda: object())
    monkeypatch.setattr("noema.agent_runtime.kalshi_runtime_credential_diagnostic", lambda _c: {})
    monkeypatch.setattr("noema.agent_runtime.KalshiTelemetry", InvalidRequestTelemetry)
    output = StringIO()
    with redirect_stdout(output):
        state = asyncio.run(_kalshi_state())

    assert state.status == "degraded"
    assert state.detail == "Kalshi request could not be formed safely"
    assert '"result": "request_protocol_failure"' in output.getvalue()
    assert "do-not-log" not in output.getvalue()
    assert "example.invalid" not in output.getvalue()
