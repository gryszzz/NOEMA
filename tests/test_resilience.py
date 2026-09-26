from noema.resilience import assess_price_resilience


def test_recovery_after_upward_shock_is_detected() -> None:
    result = assess_price_resilience(
        pre_shock=0.50,
        shocked=0.70,
        subsequent=[0.66, 0.60, 0.54],
        recovery_fraction=0.75,
    )
    assert result.resilient is True
    assert result.recovery_steps == 3
