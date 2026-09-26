from decimal import Decimal

from noema.account_reconciliation import reconcile_order_fills
from noema.trade_telemetry import ObservedFill, ObservedOrder


def test_reconciliation_detects_fill_mismatch() -> None:
    order = ObservedOrder(
        "o1", "M", "executed",
        Decimal(2), Decimal(1), Decimal(0),
        Decimal("0.5"), None,
        Decimal(0), Decimal(0), Decimal(0), Decimal(0),
        None, None, None,
    )
    fill = ObservedFill(
        "f1", "o1", "M", "yes", Decimal(2),
        Decimal("0.5"), None, True, Decimal("0.01"), None,
    )
    report = reconcile_order_fills([order], [fill])
    assert report.ok is False
    assert report.issues[0].code == "fill_count_mismatch"
