from decimal import Decimal

from noema.kalshi_telemetry import parse_fill, parse_order, parse_position


def test_order_parser_reads_realized_fee_fields() -> None:
    order = parse_order(
        {
            "order_id": "o1",
            "ticker": "M",
            "status": "resting",
            "initial_count_fp": "10.00",
            "fill_count_fp": "4.00",
            "remaining_count_fp": "6.00",
            "yes_price_dollars": "0.5600",
            "taker_fill_cost_dollars": "2.2400",
            "maker_fill_cost_dollars": "0",
            "taker_fees_dollars": "0.1200",
            "maker_fees_dollars": "0",
            "order_group_id": "g1",
        }
    )
    assert order.fill_count == Decimal("4.00")
    assert order.taker_fees == Decimal("0.1200")
    assert order.order_group_id == "g1"


def test_fill_parser_reads_actual_fee_cost() -> None:
    fill = parse_fill(
        {
            "fill_id": "f1",
            "order_id": "o1",
            "ticker": "M",
            "outcome_side": "yes",
            "count_fp": "2.00",
            "yes_price_dollars": "0.5600",
            "is_taker": True,
            "fee_cost": "0.0300",
        }
    )
    assert fill.count == Decimal("2.00")
    assert fill.fee_cost == Decimal("0.0300")


def test_position_parser_reads_exchange_pnl_and_fees() -> None:
    position = parse_position(
        {
            "ticker": "M",
            "position_fp": "2.00",
            "total_traded_dollars": "5.00",
            "market_exposure_dollars": "1.00",
            "realized_pnl_dollars": "0.20",
            "fees_paid_dollars": "0.05",
        }
    )
    assert position.realized_pnl == Decimal("0.20")
    assert position.fees_paid == Decimal("0.05")
