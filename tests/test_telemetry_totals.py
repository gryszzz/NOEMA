from decimal import Decimal

from noema.telemetry_report import build_telemetry_report
from noema.trade_telemetry import ObservedPosition


def test_report_sums_exchange_reported_pnl_and_fees() -> None:
    positions = [
        ObservedPosition(
            "A",
            Decimal(1),
            Decimal("0.50"),
            Decimal("0.50"),
            Decimal("0.20"),
            Decimal("0.03"),
            None,
        ),
        ObservedPosition(
            "B",
            Decimal(1),
            Decimal("0.60"),
            Decimal("0.60"),
            Decimal("-0.05"),
            Decimal("0.02"),
            None,
        ),
    ]
    report = build_telemetry_report(orders=[], fills=[], positions=positions)
    assert report["realized_pnl"] == Decimal("0.15")
    assert report["fees_paid"] == Decimal("0.05")
    assert report["open_exposure"] == Decimal("1.10")
