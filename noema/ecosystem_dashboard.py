from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .ecosystem_store import EcosystemStore
from .evolution_store import EvolutionStore
from .research_trials import ResearchTrialStore


def build_ecosystem_overview(
    path: str = "data/noema.db",
    *,
    trial_limit: int = 20,
) -> dict[str, Any]:
    if trial_limit <= 0:
        raise ValueError("trial_limit must be positive")
    if not Path(path).exists():
        return {
            "database_present": False,
            "specialists": [],
            "latest_plan": None,
            "recent_trials": [],
        }

    ecosystem = EcosystemStore(path)
    evolution = EvolutionStore(path)
    profiles = ecosystem.profiles()

    specialists: list[dict[str, Any]] = []
    for profile in profiles:
        state = evolution.state(profile.name)
        specialists.append(
            {
                **asdict(profile),
                "state": profile.state.value,
                "evolution": asdict(state),
                "latest_review": evolution.latest_review(profile.name),
            }
        )

    trials = [
        asdict(trial)
        for trial in ResearchTrialStore(path).recent(limit=trial_limit)
    ]

    return {
        "database_present": True,
        "specialists": specialists,
        "latest_plan": ecosystem.latest_plan(),
        "recent_trials": trials,
    }
