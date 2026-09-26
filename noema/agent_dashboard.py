from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .agent_identity import AgentIdentity
from .agent_store import AgentStore


def build_agent_overview(path: str = "data/noema.db") -> dict[str, Any]:
    store = AgentStore(path)
    identity = AgentIdentity()
    status = store.read_status(identity)
    return {
        **asdict(status),
        "principles": identity.principles,
    }
