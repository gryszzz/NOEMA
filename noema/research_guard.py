from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchEvidence:
    resolved: int
    estimated_edge: float
    edge_standard_error: float
    strategies_tested: int
    out_of_sample: bool
    lookahead_free: bool


@dataclass(frozen=True)
class ResearchVerdict:
    credible: bool
    conservative_edge: float
    z_score: float
    multiplicity_penalty: float
    reasons: tuple[str, ...]


def audit_research(
    evidence: ResearchEvidence,
    *,
    min_resolved: int = 100,
    z_multiplier: float = 1.96,
) -> ResearchVerdict:
    reasons: list[str] = []

    if evidence.resolved < min_resolved:
        reasons.append("insufficient resolved sample")
    if not evidence.out_of_sample:
        reasons.append("not out-of-sample")
    if not evidence.lookahead_free:
        reasons.append("lookahead risk")
    if evidence.edge_standard_error <= 0:
        reasons.append("edge standard error unavailable")

    z_score = (
        evidence.estimated_edge / evidence.edge_standard_error
        if evidence.edge_standard_error > 0
        else 0.0
    )

    # Conservative search penalty grows slowly with the number of attempted strategies.
    tested = max(1, evidence.strategies_tested)
    multiplicity_penalty = (
        evidence.edge_standard_error * math.sqrt(2 * math.log(tested))
        if evidence.edge_standard_error > 0
        else 0.0
    )
    conservative_edge = (
        evidence.estimated_edge
        - z_multiplier * max(evidence.edge_standard_error, 0.0)
        - multiplicity_penalty
    )

    if conservative_edge <= 0:
        reasons.append("edge does not survive uncertainty and search penalty")

    return ResearchVerdict(
        credible=not reasons,
        conservative_edge=conservative_edge,
        z_score=z_score,
        multiplicity_penalty=multiplicity_penalty,
        reasons=tuple(reasons),
    )
