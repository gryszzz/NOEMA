from decimal import Decimal

from noema.execution_models import FillState, OrderState
from noema.reconciliation import reconcile_orders_and_fills


def order(fill_count: str = "2") -> OrderState:
    return OrderState(
        "o1", None, "M", "executed", "yes", "bid",
        Decimal("2"), Decimal(fill_count), Decimal("0"),
        Decimal("0.5"), None, Decimal(0), Decimal(0),
        Decimal(0), Decimal(0), None, None, None,
    )


def fill() -> FillState:
    return FillState(
        "f1", "o1", "M", "yes", "bid", Decimal("2"),
        Decimal("0.5"), None, True, Decimal("0.01"), None,
    )


def test_reconciliation_passes_matching_counts() -> None:
    assert reconcile_orders_and_fills([order()], [fill()]).ok is True


def test_reconciliation_fails_mismatched_counts() -> None:
    result = reconcile_orders_and_fills([order("1")], [fill()])
    assert result.ok is False
    assert result.issues[0].code == "fill_count_mismatch"
