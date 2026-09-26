from decimal import Decimal

from noema.execution_economics import summarize_execution
from noema.execution_models import FillState, OrderState


def test_partial_fill_economics_are_measured_from_actual_fills() -> None:
    order = OrderState(
        "o1", None, "M", "resting", "yes", "bid",
        Decimal("10"), Decimal("4"), Decimal("6"),
        Decimal("0.56"), None, Decimal(0), Decimal(0),
        Decimal(0), Decimal(0), None, None, None,
    )
    fills = [
        FillState(
            "f1", "o1", "M", "yes", "bid", Decimal("2"),
            Decimal("0.55"), None, True, Decimal("0.02"), None,
        ),
        FillState(
            "f2", "o1", "M", "yes", "bid", Decimal("2"),
            Decimal("0.56"), None, False, Decimal("0.01"), None,
        ),
    ]
    result = summarize_execution(order, fills)
    assert result.filled_count == Decimal("4")
    assert result.remaining_count == Decimal("6")
    assert result.gross_fill_cost == Decimal("2.22")
    assert result.fees_paid == Decimal("0.03")
    assert result.maker_fraction == Decimal("0.5")
