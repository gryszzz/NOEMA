from decimal import Decimal

from noema.resource_market import (
    ResourceKind,
    ResourceProposal,
    evaluate_resource_proposal,
)


def test_strong_reversible_research_tool_can_pass() -> None:
    result = evaluate_resource_proposal(
        ResourceProposal(
            proposal_id="data-1",
            kind=ResourceKind.DATA,
            name="Premium dataset",
            one_time_cost_usd=Decimal(0),
            monthly_cost_usd=Decimal(20),
            expected_monthly_value_usd=Decimal(60),
            evidence_strength=Decimal("0.9"),
            strategic_fit=Decimal("0.9"),
            reversibility=Decimal(1),
        ),
        available_one_time_budget_usd=Decimal(50),
        available_monthly_budget_usd=Decimal(30),
    )
    assert result.approved is True


def test_expensive_weak_proposal_fails() -> None:
    result = evaluate_resource_proposal(
        ResourceProposal(
            proposal_id="rack",
            kind=ResourceKind.INFRASTRUCTURE,
            name="Large rack",
            one_time_cost_usd=Decimal(5000),
            monthly_cost_usd=Decimal(200),
            expected_monthly_value_usd=Decimal(100),
            evidence_strength=Decimal("0.2"),
            strategic_fit=Decimal("0.3"),
            reversibility=Decimal("0.1"),
        ),
        available_one_time_budget_usd=Decimal(100),
        available_monthly_budget_usd=Decimal(50),
    )
    assert result.approved is False
