from noema.strategy_health import (
    StrategyEvidence,
    StrategyStatus,
    evaluate_strategy,
)


def test_strategy_requires_evidence() -> None:
    result = evaluate_strategy(
        StrategyEvidence(
            resolved_forecasts=20,
            brier=0.15,
            market_baseline_brier=0.20,
            after_cost_return=0.10,
            max_drawdown_fraction=0.02,
        )
    )
    assert result.status is StrategyStatus.QUARANTINED


def test_profitable_calibrated_strategy_can_activate() -> None:
    result = evaluate_strategy(
        StrategyEvidence(
            resolved_forecasts=500,
            brier=0.15,
            market_baseline_brier=0.20,
            after_cost_return=0.12,
            max_drawdown_fraction=0.04,
        )
    )
    assert result.status is StrategyStatus.ACTIVE
    assert result.risk_multiplier == 1.0
