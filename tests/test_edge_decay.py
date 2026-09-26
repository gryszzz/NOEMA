from noema.edge_decay import estimate_edge_decay


def test_half_life_is_estimated() -> None:
    result = estimate_edge_decay([0.08, 0.06, 0.04, 0.02])
    assert result.half_life_steps == 2
    assert result.persistence_fraction == 1.0
