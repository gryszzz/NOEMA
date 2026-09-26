from decimal import Decimal

from noema.post_fill_analysis import measure_post_fill_move


def test_post_fill_move_is_measured() -> None:
    result = measure_post_fill_move(
        fill_price=Decimal("0.50"),
        later_mark=Decimal("0.55"),
        horizon_seconds=10,
    )
    assert result.move == Decimal("0.05")
