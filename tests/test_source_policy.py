from noema.source_policy import assess_source


def test_official_resolution_source_can_support_resolution() -> None:
    result = assess_source("official_resolution")
    assert result.admissible_for_fact is True
    assert result.admissible_for_resolution is True


def test_unknown_source_has_no_authority() -> None:
    result = assess_source("unknown")
    assert result.weight == 0.0
    assert result.admissible_for_fact is False
