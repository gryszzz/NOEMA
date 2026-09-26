from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class ResourceKind(str, Enum):
    RESEARCH = "research"
    DATA = "data"
    API = "api"
    COMPUTE = "compute"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class ResourceProposal:
    proposal_id: str
    kind: ResourceKind
    name: str
    one_time_cost_usd: Decimal
    monthly_cost_usd: Decimal
    expected_monthly_value_usd: Decimal
    evidence_strength: Decimal
    strategic_fit: Decimal
    reversibility: Decimal


@dataclass(frozen=True)
class ResourceDecision:
    approved: bool
    score: Decimal
    reasons: tuple[str, ...]


def evaluate_resource_proposal(
    proposal: ResourceProposal,
    *,
    available_one_time_budget_usd: Decimal,
    available_monthly_budget_usd: Decimal,
    minimum_score: Decimal = Decimal(1),
) -> ResourceDecision:
    for name, value in (
        ("evidence_strength", proposal.evidence_strength),
        ("strategic_fit", proposal.strategic_fit),
        ("reversibility", proposal.reversibility),
    ):
        if not Decimal(0) <= value <= Decimal(1):
            raise ValueError(f"{name} must be in [0, 1]")

    if proposal.one_time_cost_usd < 0 or proposal.monthly_cost_usd < 0:
        raise ValueError("resource costs must be non-negative")

    reasons: list[str] = []
    if proposal.one_time_cost_usd > available_one_time_budget_usd:
        reasons.append("one-time budget exceeded")
    if proposal.monthly_cost_usd > available_monthly_budget_usd:
        reasons.append("monthly budget exceeded")
    if proposal.expected_monthly_value_usd <= 0:
        reasons.append("expected value is not positive")

    monthlyized_cost = (
        proposal.one_time_cost_usd / Decimal(12)
        + proposal.monthly_cost_usd
    )
    denominator = max(monthlyized_cost, Decimal("0.01"))
    confidence = (
        proposal.evidence_strength
        * proposal.strategic_fit
        * (Decimal("0.5") + Decimal("0.5") * proposal.reversibility)
    )
    score = proposal.expected_monthly_value_usd / denominator * confidence

    if score < minimum_score:
        reasons.append("evidence-adjusted resource score below threshold")

    return ResourceDecision(
        approved=not reasons,
        score=score,
        reasons=tuple(reasons),
    )
