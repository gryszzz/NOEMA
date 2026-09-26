from decimal import Decimal

from noema.economic_bootstrap import bootstrap_economy


def test_bootstrap_partitions_starting_capital() -> None:
    snapshot = bootstrap_economy(Decimal(500))
    assert snapshot.reserve_usd == Decimal(250)
    assert snapshot.strategy_capital_usd == Decimal(200)
    assert snapshot.research_budget_usd == Decimal(50)
    assert snapshot.high_water_equity_usd == Decimal(500)
