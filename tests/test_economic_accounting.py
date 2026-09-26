from decimal import Decimal

import pytest

from noema.economic_accounting import (
    apply_profit_plan,
    execute_treasury_sweep,
    spend_operating_budget,
    validate_snapshot,
)
from noema.economic_models import CapitalBucket, EconomicSnapshot
from noema.profit_waterfall import allocate_profit


def snapshot() -> EconomicSnapshot:
    return EconomicSnapshot(
        starting_capital_usd=Decimal(500),
        current_equity_usd=Decimal(600),
        realized_net_pnl_usd=Decimal(100),
        high_water_equity_usd=Decimal(550),
        reserve_usd=Decimal(200),
        strategy_capital_usd=Decimal(250),
        research_budget_usd=Decimal(50),
        infrastructure_budget_usd=Decimal(0),
        treasury_sweep_usd=Decimal(0),
    )


def test_profit_plan_is_applied_once() -> None:
    current = snapshot()
    plan = allocate_profit(current)
    updated = apply_profit_plan(current, plan)
    assert updated.high_water_equity_usd == Decimal(600)
    assert updated.reserve_usd == Decimal("217.50")
    assert updated.treasury_sweep_usd == Decimal(5)


def test_research_spend_reduces_equity_and_budget() -> None:
    current = apply_profit_plan(snapshot(), allocate_profit(snapshot()))
    updated = spend_operating_budget(
        current,
        bucket=CapitalBucket.RESEARCH,
        amount_usd=Decimal(5),
    )
    assert updated.current_equity_usd == Decimal(595)
    assert updated.realized_net_pnl_usd == Decimal(95)


def test_treasury_sweep_is_not_recorded_as_trading_loss() -> None:
    current = apply_profit_plan(snapshot(), allocate_profit(snapshot()))
    updated = execute_treasury_sweep(current, Decimal(5))
    assert updated.current_equity_usd == Decimal(595)
    assert updated.realized_net_pnl_usd == Decimal(100)


def test_earmarks_cannot_exceed_equity() -> None:
    bad = snapshot()
    bad = EconomicSnapshot(
        **{**bad.__dict__, "reserve_usd": Decimal(700)}
    )
    with pytest.raises(ValueError, match="earmarks"):
        validate_snapshot(bad)
