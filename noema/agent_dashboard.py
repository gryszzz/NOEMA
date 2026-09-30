from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_identity import AgentIdentity
from .agent_store import AgentStore
from .runtime_diagnostics import ProcessProbe, diagnose_runtime, probe_local_process


def build_agent_overview(
    path: str = "data/noema.db",
    *,
    stale_after_seconds: float = 90.0,
    now: datetime | None = None,
    process_probe: ProcessProbe = probe_local_process,
    checkout_path: str | None = None,
) -> dict[str, Any]:
    store = AgentStore(path)
    identity = AgentIdentity()
    status = store.read_status(identity)
    process_identity = store.read_process_identity(identity.agent_id)
    store.conn.close()
    now = now or datetime.now(UTC)

    heartbeat_age_seconds: float | None = None
    alive = False
    if status.last_heartbeat_at is not None:
        heartbeat = status.last_heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        heartbeat_age_seconds = (now - heartbeat.astimezone(UTC)).total_seconds()
        if heartbeat_age_seconds < 0:
            heartbeat_age_seconds = None
    cycle_health = status.last_cycle.health if status.last_cycle else "unknown"
    diagnostics = diagnose_runtime(
        running=status.running,
        heartbeat_age_seconds=heartbeat_age_seconds,
        identity_value=process_identity,
        database_path=store.path,
        checkout_path=checkout_path or str(Path(__file__).resolve().parent.parent),
        now=now,
        stale_after_seconds=stale_after_seconds,
        process_probe=process_probe,
    )
    alive = diagnostics["state"] == "LIVE"

    return {
        **asdict(status),
        "principles": identity.principles,
        "alive": alive,
        "health": cycle_health,
        "healthy": alive and cycle_health == "healthy",
        "runtime_state": diagnostics["state"],
        "process_state": diagnostics["process_state"],
        "process_identity": diagnostics["process_identity"],
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "stale_after_seconds": stale_after_seconds,
    }
