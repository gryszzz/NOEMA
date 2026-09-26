from noema.model_trust import ModelPerformance, derive_model_trust


def test_small_sample_has_low_trust() -> None:
    trust = derive_model_trust(
        ModelPerformance(
            name="m",
            resolved=10,
            mean_log_loss=0.5,
            market_mean_log_loss=0.6,
            mean_brier=0.18,
            market_mean_brier=0.20,
        )
    )
    assert trust.reliability < 0.1


def test_strong_large_sample_gets_more_trust() -> None:
    trust = derive_model_trust(
        ModelPerformance(
            name="m",
            resolved=500,
            mean_log_loss=0.40,
            market_mean_log_loss=0.60,
            mean_brier=0.14,
            market_mean_brier=0.20,
        )
    )
    assert trust.reliability > 0.5


def test_market_equivalent_model_earns_no_trust() -> None:
    trust = derive_model_trust(
        ModelPerformance(
            name="flat",
            resolved=500,
            mean_log_loss=0.50,
            market_mean_log_loss=0.50,
            mean_brier=0.18,
            market_mean_brier=0.18,
        )
    )
    assert trust.reliability == 0.0
