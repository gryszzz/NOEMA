from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .agent_identity import AgentIdentity
from .agent_store import AgentStore


def build_agent_overview(
    path: str = "data/noema.db",
    *,
    stale_after_seconds: float = 90.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = AgentStore(path)
    identity = AgentIdentity()
    status = store.read_status(identity)
    now = now or datetime.now(UTC)

    heartbeat_age_seconds: float | None = None
    alive = False
    if status.last_heartbeat_at is not None:
        heartbeat = status.last_heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        heartbeat_age_seconds = max(
            0.0,
            (now - heartbeat.astimezone(UTC)).total_seconds(),
        )
        alive = status.running and heartbeat_age_seconds <= stale_after_seconds

    return {
        **asdict(status),
        "principles": identity.principles,
        "alive": alive,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "stale_after_seconds": stale_after_seconds,
    }
