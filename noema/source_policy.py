from __future__ import annotations

from dataclasses import dataclass


_SOURCE_WEIGHTS = {
    "official_exchange": 1.00,
    "official_resolution": 1.00,
    "primary_data": 0.95,
    "official_government": 0.95,
    "official_team_league": 0.90,
    "licensed_data": 0.90,
    "reputable_secondary": 0.65,
    "social": 0.25,
    "unknown": 0.0,
}


@dataclass(frozen=True)
class SourceAssessment:
    weight: float
    admissible_for_fact: bool
    admissible_for_resolution: bool


def assess_source(source_type: str) -> SourceAssessment:
    weight = _SOURCE_WEIGHTS.get(source_type, 0.0)
    return SourceAssessment(
        weight=weight,
        admissible_for_fact=weight >= 0.65,
        admissible_for_resolution=source_type in {
            "official_resolution",
            "official_exchange",
        },
    )
