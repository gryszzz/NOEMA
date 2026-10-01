import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

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
            duration_seconds=147.2,
            cadence_seconds=120.0,
            stage_timings={"research_allocation": 2.5, "market_collection": 121.0},
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
    assert loaded.last_cycle.duration_seconds == 147.2
    assert loaded.last_cycle.cadence_seconds == 120.0
    assert loaded.last_cycle.stage_timings["market_collection"] == 121.0


def test_cycle_timings_persist_idempotently_and_reject_invalid_values(tmp_path) -> None:
    store = AgentStore(str(tmp_path / "noema.db"))
    start = datetime.now(UTC)
    end = start + timedelta(seconds=147)
    store.append_cycle_timing(cycle_id=374, started_at=start, completed_at=end,
                              duration_seconds=147.0,
                              stage_timings={"collection": 121.0, "persistence": 0.2})
    store.append_cycle_timing(cycle_id=374, started_at=start, completed_at=end,
                              duration_seconds=148.0,
                              stage_timings={"collection": 122.0, "persistence": 0.3})
    rows = store.conn.execute(
        "SELECT cycle_id,duration_seconds,stage_timings_json FROM agent_cycle_timings"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0:2] == (374, 148.0)
    assert json.loads(rows[0][2]) == {"collection": 122.0, "persistence": 0.3}
    with pytest.raises(ValueError):
        store.append_cycle_timing(cycle_id=375, started_at=start, completed_at=end,
                                  duration_seconds=-1, stage_timings={})


def test_agent_store_wal_allows_snapshot_reader_during_runtime_write(tmp_path) -> None:
    path = tmp_path / "noema.db"
    store = AgentStore(str(path))
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    now = datetime.now(UTC)
    status = AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=now, last_cycle=None,
    )
    store.write_status(status)

    reader = sqlite3.connect(path, timeout=0.05)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT status_json FROM agent_runtime").fetchone()
        writer = sqlite3.connect(path, timeout=0.05)
        try:
            writer.execute(
                "UPDATE agent_runtime SET updated_at=? WHERE agent_id='noema'",
                (now.isoformat(),),
            )
            writer.commit()
        finally:
            writer.close()
    finally:
        reader.rollback()
        reader.close()
        store.conn.close()


def test_heartbeat_write_failure_rolls_back_its_transaction(tmp_path) -> None:
    store = AgentStore(str(tmp_path / "noema.db"))
    identity = {"pid": 42, "started_at": datetime.now(UTC).isoformat()}
    store.set_process_identity(identity)
    status = AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=datetime.now(UTC), last_cycle=None,
    )
    store.write_status(status)
    before = store.conn.execute(
        "SELECT status_json FROM agent_runtime WHERE agent_id='noema'"
    ).fetchone()[0]
    store.conn.execute(
        "CREATE TRIGGER reject_heartbeat BEFORE INSERT ON agent_heartbeats "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    store.conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        store.refresh_heartbeat()

    assert store.conn.in_transaction is False
    after = store.conn.execute(
        "SELECT status_json FROM agent_runtime WHERE agent_id='noema'"
    ).fetchone()[0]
    assert after == before
    store.conn.close()


def test_process_identity_is_persisted_only_while_runtime_is_running(tmp_path) -> None:
    store = AgentStore(str(tmp_path / "noema.db"))
    identity = {
        "pid": 123,
        "started_at": datetime.now(UTC).isoformat(),
        "checkout_path": str(tmp_path),
        "commit": "a" * 40,
        "database_path": str((tmp_path / "noema.db").resolve()),
        "working_tree_dirty": False,
    }
    store.set_process_identity(identity)
    running = AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=datetime.now(UTC), last_cycle=None,
    )
    store.write_status(running)
    store.append_heartbeat(running)
    assert store.read_process_identity() == identity
    payload = json.loads(store.conn.execute(
        "SELECT payload_json FROM agent_heartbeats ORDER BY id DESC LIMIT 1"
    ).fetchone()[0])
    assert payload["process_identity"] == identity

    stopped = AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=False, last_heartbeat_at=datetime.now(UTC), last_cycle=None,
    )
    store.write_status(stopped)
    store.append_heartbeat(stopped)
    assert store.read_process_identity() is None


def test_runtime_lifecycle_event_is_visible_in_workstation_event_table(tmp_path) -> None:
    store = AgentStore(str(tmp_path / "noema.db"))
    store.append_runtime_event(
        "runtime:123:2026-09-29T22:00:00+00:00",
        "agent_cycle", "completed", '{"cycle_id": 1}',
    )

    event = store.conn.execute(
        "SELECT session_id,stage,status,tool,detail FROM runtime_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event == (
        "runtime:123:2026-09-29T22:00:00+00:00",
        "agent_cycle", "completed", "noema-agent", '{"cycle_id": 1}',
    )


def test_threaded_heartbeat_refresh_preserves_runtime_identity(tmp_path) -> None:
    path = tmp_path / "noema.db"
    identity = {
        "pid": 123, "started_at": datetime.now(UTC).isoformat(),
        "checkout_path": str(tmp_path), "commit": "a" * 40,
        "database_path": str(path.resolve()), "working_tree_dirty": True,
    }
    writer = AgentStore(str(path))
    writer.set_process_identity(identity)
    writer.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=datetime.now(UTC), last_cycle=None,
    ))
    heartbeat_writer = AgentStore(str(path))
    heartbeat_writer.set_process_identity(identity)
    updated_at = datetime.now(UTC)

    assert heartbeat_writer.refresh_heartbeat(updated_at) is True
    status = writer.read_status()
    assert status.running is True
    assert status.last_heartbeat_at == updated_at
    assert heartbeat_writer.read_process_identity() == identity


def test_threaded_heartbeat_cannot_refresh_a_different_process_identity(tmp_path) -> None:
    path = tmp_path / "noema.db"
    writer = AgentStore(str(path))
    identity = {
        "pid": 123, "started_at": datetime.now(UTC).isoformat(),
        "checkout_path": str(tmp_path), "commit": "a" * 40,
        "database_path": str(path.resolve()), "working_tree_dirty": False,
    }
    writer.set_process_identity(identity)
    written_at = datetime.now(UTC)
    writer.write_status(AgentStatus(
        agent_id="noema", name="NOEMA", version="0.1.0", mission="test",
        running=True, last_heartbeat_at=written_at, last_cycle=None,
    ))
    before = writer.conn.execute(
        "SELECT updated_at FROM agent_runtime WHERE agent_id='noema'",
    ).fetchone()[0]
    other_writer = AgentStore(str(path))
    other_writer.set_process_identity({**identity, "pid": 456})

    assert other_writer.refresh_heartbeat(written_at + timedelta(seconds=15)) is False
    after = writer.conn.execute(
        "SELECT updated_at FROM agent_runtime WHERE agent_id='noema'",
    ).fetchone()[0]
    assert after == before
