from decimal import Decimal

from noema.telemetry_report import build_telemetry_report
from noema.trade_telemetry import ObservedFill, ObservedOrder, ObservedPosition


def test_report_surfaces_reconciliation() -> None:
    order = ObservedOrder(
        "o1", "M", "executed",
        Decimal(1), Decimal(1), Decimal(0),
        Decimal("0.5"), None,
        Decimal(0), Decimal(0), Decimal(0), Decimal(0),
        None, None, None,
    )
    fill = ObservedFill(
        "f1", "o1", "M", "yes", Decimal(1),
        Decimal("0.5"), None, True, Decimal("0.01"), None,
    )
    position = ObservedPosition(
        "M", Decimal(1), Decimal("0.5"), Decimal("0.5"),
        Decimal(0), Decimal("0.01"), None,
    )
    report = build_telemetry_report(
        orders=[order],
        fills=[fill],
        positions=[position],
    )
    assert report["order_fill_reconciliation"]["ok"] is True
    assert report["position_fee_reconciliation"]["ok"] is True
