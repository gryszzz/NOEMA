from noema.tail_calibration import evaluate_tail


def test_upper_tail_calibration_detects_overconfidence() -> None:
    result = evaluate_tail([(0.9, 1), (0.9, 0), (0.9, 0)], upper_tail=True)
    assert result.count == 3
    assert result.signed_error > 0
