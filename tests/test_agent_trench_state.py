import pytest

from noema import agent_runtime
from noema.agent_runtime import _trench_state
from noema.trench_collector import TrenchCollectorStore


@pytest.mark.asyncio
async def test_agent_trench_state_is_disabled_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("NOEMA_TRENCH_ENABLED", raising=False)
    state = await _trench_state(str(tmp_path / "noema.db"))
    assert state.status == "disabled"


@pytest.mark.asyncio
async def test_agent_trench_state_reads_persisted_health_without_collecting(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NOEMA_TRENCH_ENABLED", "1")
    db_path = str(tmp_path / "noema.db")
    store = TrenchCollectorStore(db_path)
    for provider in ("jupiter_discovery", "jupiter_market", "jupiter_price",
                     "dexscreener_market"):
        store.record_provider_health(provider, success=True)
    store.record_provider_health("solana_rpc:primary", success=False,
                                 error_class="rate_limited")
    store.conn.close()

    async def duplicate_collection_must_not_run(**_kwargs):
        raise AssertionError("health check must not perform a second collection")

    monkeypatch.setattr(agent_runtime, "collect_trench_cycle",
                        duplicate_collection_must_not_run)
    state = await _trench_state(db_path)
    assert state.status == "connected"
    assert "forward_sampler=independent" in state.detail
    assert "optional_rpc_failures=solana_rpc:primary" in state.detail
    assert "last_error_class:rate_limited" in state.detail


@pytest.mark.asyncio
async def test_agent_trench_state_is_degraded_until_core_sampler_health_exists(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("NOEMA_TRENCH_ENABLED", "1")
    state = await _trench_state(str(tmp_path / "noema.db"))
    assert state.status == "degraded"
    assert "core_providers=degraded" in state.detail
    assert "provider_health=not recorded yet" in state.detail
