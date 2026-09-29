import httpx
import pytest

from noema import agent_runtime
from noema.agent_runtime import _trench_state


@pytest.mark.asyncio
async def test_agent_trench_state_is_disabled_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NOEMA_TRENCH_ENABLED", raising=False)
    state = await _trench_state(str(tmp_path / "noema.db"))
    assert state.status == "disabled"


@pytest.mark.asyncio
async def test_agent_trench_state_reports_provider_http_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_TRENCH_ENABLED", "1")
    request = httpx.Request("GET", "https://api.jup.ag/tokens/v2/recent")
    response = httpx.Response(429, request=request)

    async def fail(**_kwargs):
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    monkeypatch.setattr(agent_runtime, "collect_trench_cycle", fail)
    state = await _trench_state(str(tmp_path / "noema.db"))
    assert state.status == "degraded"
    assert "Jupiter returned HTTP 429" in state.detail
