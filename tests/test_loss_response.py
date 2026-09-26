from decimal import Decimal

from noema.loss_response import loss_response


def test_moderate_drawdown_reduces_risk() -> None:
    result = loss_response(
        drawdown_fraction=Decimal("0.05"),
        daily_loss_fraction=Decimal(0),
        consecutive_losing_days=0,
    )
    assert result.risk_multiplier == Decimal("0.50")
    assert result.research_only is False


def test_severe_loss_forces_research_only() -> None:
    result = loss_response(
        drawdown_fraction=Decimal("0.11"),
        daily_loss_fraction=Decimal(0),
        consecutive_losing_days=0,
    )
    assert result.risk_multiplier == Decimal(0)
    assert result.research_only is True
