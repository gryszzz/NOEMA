from decimal import Decimal

from noema.autonomy import earned_autonomy
from noema.economic_models import AutonomyEvidence, AutonomyLevel


def evidence(**overrides) -> AutonomyEvidence:
    values = {
        "resolved_forecasts": 1500,
        "live_observation_days": 90,
        "after_cost_return": Decimal("0.05"),
        "realized_net_pnl_usd": Decimal(500),
        "max_drawdown_fraction": Decimal("0.04"),
        "calibration_error": Decimal("0.03"),
        "reconciliation_ok_fraction": Decimal("0.999"),
        "profitable_days": 50,
        "losing_days": 20,
    }
    values.update(overrides)
    return AutonomyEvidence(**values)


def test_strong_evidence_can_earn_self_funded_level() -> None:
    result = earned_autonomy(evidence())
    assert result.earned_level is AutonomyLevel.SELF_FUNDED


def test_drawdown_blocks_micro_promotion() -> None:
    result = earned_autonomy(
        evidence(max_drawdown_fraction=Decimal("0.20"))
    )
    assert result.earned_level in {
        AutonomyLevel.DEMO,
        AutonomyLevel.PAPER,
        AutonomyLevel.SHADOW,
    }
