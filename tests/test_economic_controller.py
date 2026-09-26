from decimal import Decimal

from noema.economic_controller import EconomicController
from noema.economic_ledger import EconomicLedger
from noema.economic_models import AutonomyEvidence, AutonomyLevel, EconomicSnapshot


def test_controller_promotes_one_level_and_records_review(tmp_path) -> None:
    ledger = EconomicLedger(str(tmp_path / "noema.db"))
    controller = EconomicController(ledger)
    snapshot = EconomicSnapshot(
        starting_capital_usd=Decimal(500),
        current_equity_usd=Decimal(600),
        realized_net_pnl_usd=Decimal(100),
        high_water_equity_usd=Decimal(550),
        reserve_usd=Decimal(200),
        strategy_capital_usd=Decimal(250),
        research_budget_usd=Decimal(50),
        infrastructure_budget_usd=Decimal(0),
        treasury_sweep_usd=Decimal(0),
    )
    evidence = AutonomyEvidence(
        resolved_forecasts=1500,
        live_observation_days=90,
        after_cost_return=Decimal("0.05"),
        realized_net_pnl_usd=Decimal(500),
        max_drawdown_fraction=Decimal("0.04"),
        calibration_error=Decimal("0.03"),
        reconciliation_ok_fraction=Decimal("0.999"),
        profitable_days=50,
        losing_days=20,
    )

    review = controller.review(
        snapshot,
        evidence,
        current_level=AutonomyLevel.PAPER,
    )

    assert review.evidence_level is AutonomyLevel.SELF_FUNDED
    assert review.transition.next_level is AutonomyLevel.DEMO
    assert review.profit_plan.profit_above_high_water_usd == Decimal(50)
