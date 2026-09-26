from noema.research_sizing import binary_kelly_fraction


def test_research_sizing_is_capped() -> None:
    result = binary_kelly_fraction(
        probability=0.70,
        price=0.50,
        fraction=0.25,
        hard_cap=0.01,
    )
    assert result.full_kelly_fraction > result.fractional_kelly_fraction
    assert result.capped_fraction == 0.01
