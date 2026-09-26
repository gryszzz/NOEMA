from noema.market_graph import (
    ProbabilityNode,
    check_exhaustive_partition,
    check_monotonic_thresholds,
)


def test_partition_gap_is_detected() -> None:
    violations = check_exhaustive_partition(
        [ProbabilityNode("a", 0.6), ProbabilityNode("b", 0.6)]
    )
    assert violations
    assert violations[0].kind == "partition_sum"


def test_threshold_monotonicity_is_detected() -> None:
    violations = check_monotonic_thresholds([(10, 0.4), (20, 0.5)])
    assert violations
    assert violations[0].kind == "monotonicity"
