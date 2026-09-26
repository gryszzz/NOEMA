from noema.lead_lag import find_lead_lag


def test_detects_simple_one_step_lead() -> None:
    leader = [0, 1, 1, 2, 2, 3, 3]
    follower = [0, 0, 1, 1, 2, 2, 3]
    result = find_lead_lag(leader, follower, max_lag=2)
    assert result.best_lag == 1
    assert result.correlation > 0.99
