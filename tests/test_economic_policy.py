from decimal import Decimal

from noema.economic_models import AutonomyLevel
from noema.economic_policy import limits_for_level


def test_autonomy_limits_expand_gradually() -> None:
    micro = limits_for_level(AutonomyLevel.MICRO)
    proven = limits_for_level(AutonomyLevel.PROVEN)
    assert proven.max_transaction_usd > micro.max_transaction_usd
    assert proven.max_daily_notional_usd > micro.max_daily_notional_usd
    assert micro.max_transaction_usd == Decimal(5)
