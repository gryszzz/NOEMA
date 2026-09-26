from decimal import Decimal

from noema.fee_history import parse_event_fee_change, parse_series_fee_change


def test_series_fee_change_parser() -> None:
    row = parse_series_fee_change(
        {
            "id": "x",
            "series_ticker": "S",
            "fee_type": "quadratic",
            "fee_multiplier": 1.5,
            "scheduled_ts": "2026-01-01T00:00:00+00:00",
        }
    )
    assert row.fee_multiplier == Decimal("1.5")


def test_event_fee_override_can_be_cleared() -> None:
    row = parse_event_fee_change(
        {
            "id": "x",
            "event_ticker": "E",
            "fee_type_override": None,
            "fee_multiplier_override": None,
            "scheduled_ts": "2026-01-01T00:00:00+00:00",
        }
    )
    assert row.fee_multiplier_override is None
