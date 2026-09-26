from noema.calibration_drift import detect_brier_drift


def test_worsening_brier_triggers_drift() -> None:
    result = detect_brier_drift(0.15, [0.22, 0.23, 0.21], allowance=0.0, threshold=0.05)
    assert result.drifted is True
    assert result.direction == "worsening"
