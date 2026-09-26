from noema.decision_quality import assess_decision_quality, binary_entropy


def test_entropy_is_maximal_at_half() -> None:
    assert binary_entropy(0.5) == 1.0
    assert binary_entropy(0.9) < 1.0


def test_narrower_interval_has_more_conviction() -> None:
    narrow = assess_decision_quality(
        probability_yes=0.70,
        market_probability=0.50,
        lower_bound=0.66,
        upper_bound=0.74,
    )
    wide = assess_decision_quality(
        probability_yes=0.70,
        market_probability=0.50,
        lower_bound=0.50,
        upper_bound=0.90,
    )
    assert narrow.conviction_score > wide.conviction_score
