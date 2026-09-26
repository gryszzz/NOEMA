from __future__ import annotations

from typing import Any

from .cognition_store import CognitionStore
from .foundry_config import FoundryConfig


def build_cognition_overview(path: str = "data/noema.db") -> dict[str, Any]:
    config = FoundryConfig.from_env()
    store = CognitionStore(path)
    return {
        "enabled": config.enabled,
        "configured": config.ready,
        "deployment": config.deployment,
        "reasoning_effort": config.reasoning_effort,
        "calls_last_hour": store.calls_last_hour(),
        "latest": store.latest(),
    }
