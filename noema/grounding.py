from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import Forecast
from .provenance import EvidenceStore


@dataclass(frozen=True)
class GroundingPolicy:
    require_evidence: bool = True
    max_evidence_age_seconds: float = 300.0
    allowed_source_types: frozenset[str] | None = None


@dataclass(frozen=True)
class GroundingResult:
    grounded: bool
    reasons: tuple[str, ...]


def validate_forecast_grounding(
    forecast: Forecast,
    store: EvidenceStore,
    *,
    policy: GroundingPolicy | None = None,
    now: datetime | None = None,
) -> GroundingResult:
    policy = policy or GroundingPolicy()
    now = now or datetime.now(UTC)
    reasons: list[str] = []

    if policy.require_evidence and not forecast.evidence_ids:
        reasons.append("forecast has no evidence ids")
        return GroundingResult(False, tuple(reasons))

    for evidence_id in forecast.evidence_ids:
        record = store.get(evidence_id)
        if record is None:
            reasons.append(f"missing evidence: {evidence_id}")
            continue

        if not store.verify_integrity(evidence_id):
            reasons.append(f"evidence integrity failure: {evidence_id}")
            continue

        age = (now - record.observed_at).total_seconds()
        if age < 0:
            reasons.append(f"future-dated evidence: {evidence_id}")
        elif age > policy.max_evidence_age_seconds:
            reasons.append(f"stale evidence: {evidence_id}")

        if (
            policy.allowed_source_types is not None
            and record.source_type not in policy.allowed_source_types
        ):
            reasons.append(f"disallowed source type: {record.source_type}")

    return GroundingResult(grounded=not reasons, reasons=tuple(reasons))
