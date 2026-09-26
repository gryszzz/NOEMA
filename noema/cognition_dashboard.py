from __future__ import annotations

from typing import Any

from .cognition_store import CognitionStore
from .foundry_config import FoundryConfig
from .research_queue import ResearchQueueStore


def build_cognition_overview(path: str = "data/noema.db") -> dict[str, Any]:
    config = FoundryConfig.from_env()
    store = CognitionStore(path)
    queue = ResearchQueueStore(path)
    pending = queue.pending(limit=10)
    return {
        "enabled": config.enabled,
        "configured": config.ready,
        "deployment": config.deployment,
        "reasoning_effort": config.reasoning_effort,
        "calls_last_hour": store.calls_last_hour(),
        "tokens_last_hour": store.tokens_last_hour(),
        "latest": store.latest(),
        "pending_research_count": queue.pending_count(),
        "pending_research": [task.__dict__ for task in pending],
    }
