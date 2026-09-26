from decimal import Decimal

from noema.fill_accounting import summarize_fills
from noema.trade_telemetry import ObservedFill, ObservedOrder


def test_partial_fill_accounting() -> None:
    order = ObservedOrder(
        "o1", "M", "resting",
        Decimal(10), Decimal(4), Decimal(6),
        Decimal("0.56"), None,
        Decimal(0), Decimal(0), Decimal(0), Decimal(0),
        None, None, None,
    )
    fills = [
        ObservedFill(
            "f1", "o1", "M", "yes", Decimal(2),
            Decimal("0.55"), None, True, Decimal("0.02"), None,
        ),
        ObservedFill(
            "f2", "o1", "M", "yes", Decimal(2),
            Decimal("0.56"), None, False, Decimal("0.01"), None,
        ),
    ]
    result = summarize_fills(order, fills)
    assert result.observed_filled_count == Decimal(4)
    assert result.remaining_count == Decimal(6)
    assert result.gross_fill_cost == Decimal("2.22")
    assert result.fees_paid == Decimal("0.03")
    assert result.maker_fraction == Decimal("0.5")
