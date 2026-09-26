import pytest

from noema.diversity import WeightedSignal, cap_family_concentration


def test_family_concentration_is_capped() -> None:
    result = cap_family_concentration(
        [
            WeightedSignal("a", "news", 10.0),
            WeightedSignal("b", "news", 10.0),
            WeightedSignal("c", "quant", 5.0),
        ],
        max_family_fraction=0.50,
    )
    assert sum(result.values()) == pytest.approx(1.0)
    assert result["c"] > 0
