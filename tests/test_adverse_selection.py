from decimal import Decimal

from noema.adverse_selection import measure_adverse_selection


def test_post_fill_mark_up_is_favorable_for_bought_contract() -> None:
    result = measure_adverse_selection(
        outcome_side="yes",
        fill_price=Decimal("0.50"),
        mark_price=Decimal("0.55"),
        horizon_seconds=10,
    )
    assert result.favorable is True
    assert result.signed_move == Decimal("0.05")
