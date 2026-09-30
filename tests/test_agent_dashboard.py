from datetime import UTC, datetime, timedelta

from noema.agent_dashboard import build_agent_overview
from noema.agent_models import AgentConnectionState, AgentCycleState, AgentStatus
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
    assert overview["runtime_state"] == "STALE"
    assert overview["heartbeat_age_seconds"] > 90


def test_future_heartbeat_and_degraded_cycle_never_look_healthy(tmp_path) -> None:
    path = str(tmp_path / "noema.db")
    store = AgentStore(path)
    future = datetime.now(UTC) + timedelta(minutes=2)
    store.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=future, last_cycle=None,
    ))
    store.conn.execute("UPDATE agent_runtime SET updated_at=? WHERE agent_id=?",
                       (future.isoformat(), "noema"))
    store.conn.commit()
    overview = build_agent_overview(path, now=datetime.now(UTC))
    assert overview["alive"] is False
    assert overview["healthy"] is False
    assert overview["runtime_state"] == "UNKNOWN"
    assert overview["heartbeat_age_seconds"] is None

    now = datetime.now(UTC)
    store = AgentStore(path)
    store.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=now,
        last_cycle=AgentCycleState(
            cycle_id=2, started_at=now-timedelta(seconds=2), completed_at=now,
            active_goal="bounded research", health="degraded",
            kalshi=AgentConnectionState("healthy"),
            evm_wallet=AgentConnectionState("unconfigured"), radar_markets=0,
            economic_state="uninitialized",
        ),
    ))
    overview = build_agent_overview(path, now=datetime.now(UTC))
    assert overview["alive"] is False
    assert overview["runtime_state"] == "UNKNOWN"
    assert overview["health"] == "degraded"
    assert overview["healthy"] is False


def test_agent_dashboard_requires_matching_live_process_identity(tmp_path) -> None:
    path = tmp_path / "noema.db"
    now = datetime.now(UTC)
    store = AgentStore(str(path))
    store.set_process_identity({
        "pid": 123, "started_at": (now-timedelta(seconds=10)).isoformat(),
        "checkout_path": str(tmp_path), "commit": "a" * 40,
        "database_path": str(path.resolve()),
    })
    store.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=now, last_cycle=None,
    ))

    overview = build_agent_overview(
        str(path), now=datetime.now(UTC) + timedelta(seconds=1),
        process_probe=lambda _identity, _now: "PROCESS MATCH",
        checkout_path=str(tmp_path),
    )

    assert overview["alive"] is True
    assert overview["runtime_state"] == "LIVE"
    assert overview["process_identity"]["pid"] == 123


def test_agent_dashboard_distinguishes_missing_process_from_stale_heartbeat(tmp_path) -> None:
    path = tmp_path / "noema.db"
    now = datetime.now(UTC)
    stale = now - timedelta(minutes=5)
    store = AgentStore(str(path))
    store.set_process_identity({
        "pid": 123, "started_at": (stale-timedelta(minutes=1)).isoformat(),
        "checkout_path": str(tmp_path), "commit": "a" * 40,
        "database_path": str(path.resolve()),
    })
    store.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=stale, last_cycle=None,
    ))
    store.conn.execute("UPDATE agent_runtime SET updated_at=? WHERE agent_id=?",
                       (stale.isoformat(), "noema"))
    store.conn.commit()

    overview = build_agent_overview(
        str(path), now=now,
        process_probe=lambda _identity, _now: "PROCESS MISSING",
        checkout_path=str(tmp_path),
    )

    assert overview["alive"] is False
    assert overview["runtime_state"] == "PROCESS MISSING"
    assert overview["process_state"] == "PROCESS MISSING"


def test_dashboard_server_alive_does_not_make_dead_agent_live(tmp_path) -> None:
    path = tmp_path / "noema.db"
    now = datetime.now(UTC)
    store = AgentStore(str(path))
    store.set_process_identity({
        "pid": 123, "started_at": (now-timedelta(seconds=10)).isoformat(),
        "checkout_path": str(tmp_path), "commit": "a" * 40,
        "database_path": str(path.resolve()), "working_tree_dirty": False,
    })
    store.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=now, last_cycle=None,
    ))

    overview = build_agent_overview(
        str(path), now=now + timedelta(seconds=1),
        process_probe=lambda _identity, _now: "PROCESS MISSING",
        checkout_path=str(tmp_path),
    )

    assert overview["alive"] is False
    assert overview["runtime_state"] == "PROCESS MISSING"


def test_gracefully_stopped_agent_is_reported_offline(tmp_path) -> None:
    path = tmp_path / "noema.db"
    store = AgentStore(str(path))
    store.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=False, last_heartbeat_at=datetime.now(UTC), last_cycle=None,
    ))

    overview = build_agent_overview(str(path), now=datetime.now(UTC))

    assert overview["alive"] is False
    assert overview["runtime_state"] == "STOPPED"
