from noema.lead_lag import find_lead_lag


def test_detects_simple_one_step_lead() -> None:
    leader = [1, 2, 3, 4, 5, 6]
    follower = [0, 1, 2, 3, 4, 5]
    result = find_lead_lag(leader, follower, max_lag=2)
    assert result.best_lag == 1
    assert result.correlation > 0.99
