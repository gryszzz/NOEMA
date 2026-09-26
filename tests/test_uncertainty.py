from noema.uncertainty import UncertaintyInputs, assess_uncertainty


def test_uncertainty_penalty_increases_with_bad_conditions() -> None:
    good = assess_uncertainty(
        UncertaintyInputs(
            interval_width=0.06,
            model_disagreement=0.02,
            data_staleness_seconds=1,
            liquidity_usd=100_000,
            spread=0.02,
        )
    )
    bad = assess_uncertainty(
        UncertaintyInputs(
            interval_width=0.20,
            model_disagreement=0.20,
            data_staleness_seconds=60,
            liquidity_usd=500,
            spread=0.10,
        )
    )
    assert bad.penalty > good.penalty
