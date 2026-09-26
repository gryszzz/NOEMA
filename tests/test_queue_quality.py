from decimal import Decimal

from noema.queue_quality import assess_queue_quality


def test_queue_quality_declines_with_large_queue() -> None:
    good = assess_queue_quality(
        contracts_ahead=Decimal("5"),
        own_remaining=Decimal("10"),
    )
    bad = assess_queue_quality(
        contracts_ahead=Decimal("100"),
        own_remaining=Decimal("10"),
    )
    assert good.score > bad.score
