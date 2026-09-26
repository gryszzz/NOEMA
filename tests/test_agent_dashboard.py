from datetime import UTC, datetime, timedelta

from noema.agent_dashboard import build_agent_overview
from noema.agent_models import AgentStatus
from noema.agent_store import AgentStore


def test_agent_dashboard_marks_stale_runtime_offline(tmp_path) -> None:
    path = str(tmp_path / "noema.db")
    store = AgentStore(path)
    old = datetime.now(UTC) - timedelta(minutes=5)
    store.write_status(
        AgentStatus(
            agent_id="noema",
            name="NOEMA",
            version="0.1.0",
            mission="test",
            running=True,
            last_heartbeat_at=old,
            last_cycle=None,
        )
    )
    # Rewrite DB timestamp to simulate a process that died after its last heartbeat.
    store.conn.execute(
        "UPDATE agent_runtime SET updated_at = ? WHERE agent_id = ?",
        (old.isoformat(), "noema"),
    )
    store.conn.commit()

    overview = build_agent_overview(
        path,
        stale_after_seconds=90,
        now=datetime.now(UTC),
    )
    assert overview["alive"] is False
    assert overview["heartbeat_age_seconds"] > 90
