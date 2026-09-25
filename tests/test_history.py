from noema.venues.kalshi_history import resolved_outcome


def test_resolved_outcome() -> None:
    assert resolved_outcome({"result": "yes"}) == 1
    assert resolved_outcome({"result": "NO"}) == 0
    assert resolved_outcome({"result": "scalar"}) is None
    assert resolved_outcome({}) is None
