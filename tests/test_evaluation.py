import math

from noema.evaluation import (
    calibration_buckets,
    expected_calibration_error,
    score_forecast,
)


def test_perfect_forecast_scores_near_zero() -> None:
    score = score_forecast(0.999, 1)
    assert score.brier < 0.00001
    assert score.log_loss < 0.01


def test_wrong_confident_forecast_is_penalized() -> None:
    score = score_forecast(0.99, 0)
    assert score.brier > 0.9
    assert score.log_loss > 4.0


def test_calibration_bucket() -> None:
    rows = [(0.8, 1), (0.8, 1), (0.8, 0), (0.8, 1)]
    bucket = calibration_buckets(rows)[0]
    assert bucket.count == 4
    assert math.isclose(bucket.mean_probability, 0.8)
    assert math.isclose(bucket.observed_rate, 0.75)
    assert math.isclose(expected_calibration_error(rows), 0.05)
