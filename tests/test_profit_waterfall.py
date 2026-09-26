from decimal import Decimal

from noema.economic_models import CapitalBucket, EconomicSnapshot
from noema.profit_waterfall import allocate_profit


def test_only_profit_above_high_water_is_allocated() -> None:
    snapshot = EconomicSnapshot(
        starting_capital_usd=Decimal(500),
        current_equity_usd=Decimal(700),
        realized_net_pnl_usd=Decimal(200),
        high_water_equity_usd=Decimal(650),
        reserve_usd=Decimal(200),
        strategy_capital_usd=Decimal(200),
        research_budget_usd=Decimal(50),
        infrastructure_budget_usd=Decimal(0),
        treasury_sweep_usd=Decimal(0),
    )
    allocation = allocate_profit(snapshot)
    assert allocation.profit_above_high_water_usd == Decimal(50)
    assert sum(allocation.allocations.values(), Decimal(0)) == Decimal("50.00")
    assert allocation.allocations[CapitalBucket.RESERVE] == Decimal("17.50")


def test_waterfall_conserves_awkward_cents() -> None:
    snapshot = EconomicSnapshot(
        starting_capital_usd=Decimal(500),
        current_equity_usd=Decimal("551.01"),
        realized_net_pnl_usd=Decimal("51.01"),
        high_water_equity_usd=Decimal(550),
        reserve_usd=Decimal(200),
        strategy_capital_usd=Decimal(250),
        research_budget_usd=Decimal(50),
        infrastructure_budget_usd=Decimal(0),
        treasury_sweep_usd=Decimal(0),
    )
    allocation = allocate_profit(snapshot)
    assert sum(allocation.allocations.values(), Decimal(0)) == Decimal("1.01")
