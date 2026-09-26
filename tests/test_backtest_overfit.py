from noema.backtest_overfit import (
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
)


def test_probabilistic_sharpe_rewards_consistent_positive_returns() -> None:
    audit = probabilistic_sharpe_ratio(
        [0.01, 0.02, -0.005, 0.015, 0.01, 0.03, -0.002, 0.01]
    )

    assert audit.sharpe > 0
    assert 0.5 < audit.probabilistic_sharpe <= 1


def test_pbo_returns_bounded_diagnostic() -> None:
    audit = probability_of_backtest_overfitting(
        {
            "steady": [0.01, 0.02, 0.01, 0.00, 0.01, 0.02, 0.01, 0.00],
            "early_luck": [0.20, 0.20, 0.20, 0.20, -0.20, -0.20, -0.20, -0.20],
            "late_luck": [-0.20, -0.20, -0.20, -0.20, 0.20, 0.20, 0.20, 0.20],
        },
        slices=4,
    )

    assert audit.splits > 0
    assert 0 <= audit.probability_backtest_overfit <= 1
    assert len(audit.selected_oos_percentiles) == audit.splits
