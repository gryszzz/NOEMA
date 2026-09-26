from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class InfrastructureProposal:
    proposal_id: str
    name: str
    one_time_cost_usd: Decimal
    monthly_cost_usd: Decimal
    expected_monthly_value_usd: Decimal
    evidence_strength: Decimal


@dataclass(frozen=True)
class InfrastructureDecision:
    affordable: bool
    score: Decimal
    reasons: tuple[str, ...]


def evaluate_infrastructure(
    proposal: InfrastructureProposal,
    *,
    available_budget_usd: Decimal,
    monthly_budget_usd: Decimal,
) -> InfrastructureDecision:
    reasons: list[str] = []
    if proposal.one_time_cost_usd < 0 or proposal.monthly_cost_usd < 0:
        raise ValueError("infrastructure costs must be non-negative")
    if not Decimal(0) <= proposal.evidence_strength <= Decimal(1):
        raise ValueError("evidence_strength must be in [0, 1]")

    if proposal.one_time_cost_usd > available_budget_usd:
        reasons.append("one-time infrastructure budget exceeded")
    if proposal.monthly_cost_usd > monthly_budget_usd:
        reasons.append("monthly infrastructure budget exceeded")
    if proposal.expected_monthly_value_usd <= 0:
        reasons.append("no positive expected monthly value")

    denominator = max(
        proposal.one_time_cost_usd / Decimal(12) + proposal.monthly_cost_usd,
        Decimal("0.01"),
    )
    score = (
        proposal.expected_monthly_value_usd
        / denominator
        * proposal.evidence_strength
    )
    if score < Decimal(1):
        reasons.append("evidence-adjusted value/cost below 1")

    return InfrastructureDecision(
        affordable=not reasons,
        score=score,
        reasons=tuple(reasons),
    )
