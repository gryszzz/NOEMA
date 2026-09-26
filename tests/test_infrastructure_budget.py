from decimal import Decimal

from noema.infrastructure_budget import (
    InfrastructureProposal,
    evaluate_infrastructure,
)


def test_good_evidence_backed_infrastructure_can_pass() -> None:
    result = evaluate_infrastructure(
        InfrastructureProposal(
            proposal_id="gpu",
            name="Research node",
            one_time_cost_usd=Decimal(500),
            monthly_cost_usd=Decimal(20),
            expected_monthly_value_usd=Decimal(100),
            evidence_strength=Decimal("0.9"),
        ),
        available_budget_usd=Decimal(600),
        monthly_budget_usd=Decimal(50),
    )
    assert result.affordable is True


def test_infrastructure_over_budget_fails() -> None:
    result = evaluate_infrastructure(
        InfrastructureProposal(
            proposal_id="rack",
            name="Server rack",
            one_time_cost_usd=Decimal(5000),
            monthly_cost_usd=Decimal(200),
            expected_monthly_value_usd=Decimal(100),
            evidence_strength=Decimal("0.5"),
        ),
        available_budget_usd=Decimal(100),
        monthly_budget_usd=Decimal(50),
    )
    assert result.affordable is False
