from datetime import UTC, datetime

from noema.agent_models import AgentConnectionState, AgentCycleState, AgentStatus
from noema.agent_store import AgentStore


def test_agent_status_roundtrip(tmp_path) -> None:
    store = AgentStore(str(tmp_path / "noema.db"))
    now = datetime.now(UTC)
    status = AgentStatus(
        agent_id="noema",
        name="NOEMA",
        version="0.1.0",
        mission="test",
        running=True,
        last_heartbeat_at=now,
        last_cycle=AgentCycleState(
            cycle_id=1,
            started_at=now,
            completed_at=now,
            active_goal="collect_world_state",
            health="healthy",
            kalshi=AgentConnectionState("connected"),
            evm_wallet=AgentConnectionState("connected"),
            radar_markets=0,
            economic_state="initialized",
            ecosystem_state="active",
            ecosystem_focus="trench-1",
            note=None,
            market_data=AgentConnectionState("connected", "valid=5"),
        ),
    )
    store.write_status(status)
    loaded = store.read_status()
    assert loaded.running is True
    assert loaded.last_cycle is not None
    assert loaded.last_cycle.cycle_id == 1
    assert loaded.last_cycle.market_data.status == "connected"
    assert loaded.last_cycle.ecosystem_state == "active"
    assert loaded.last_cycle.ecosystem_focus == "trench-1"
