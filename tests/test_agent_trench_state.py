import pytest

from noema.agent_runtime import _trench_state


@pytest.mark.asyncio
async def test_agent_trench_state_is_disabled_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NOEMA_TRENCH_ENABLED", raising=False)
    state = await _trench_state(str(tmp_path / "noema.db"))
    assert state.status == "disabled"
