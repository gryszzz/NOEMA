from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from noema.autonomy import earned_autonomy
from noema.cognition_store import CognitionStore
from noema.economic_accounting import apply_profit_plan
from noema.economic_bootstrap import bootstrap_economy
from noema.economic_ledger import EconomicLedger
from noema.economic_models import AutonomyEvidence, AutonomyLevel, CapitalBucket, ProfitAllocation
from noema.profit_waterfall import ProfitWaterfallPolicy, allocate_profit


def test_unrealized_equity_and_funding_are_not_allocatable_profit():
    funded = replace(bootstrap_economy(Decimal(500)), current_equity_usd=Decimal(600))
    assert allocate_profit(funded).profit_above_high_water_usd == 0
    partial = replace(funded, realized_net_pnl_usd=Decimal(20))
    plan = allocate_profit(partial)
    assert plan.profit_above_high_water_usd == 20
    updated = apply_profit_plan(partial, plan)
    assert updated.realized_profit_high_water_usd == 20
    assert allocate_profit(updated).profit_above_high_water_usd == 0
    realized_later = replace(updated, realized_net_pnl_usd=Decimal(100))
    assert allocate_profit(realized_later).profit_above_high_water_usd == 80
    with pytest.raises(ValueError):
        apply_profit_plan(updated, plan)


def test_unearmarked_equity_and_tiny_allocations_are_conserved():
    current = replace(bootstrap_economy(Decimal(500)),
                      current_equity_usd=Decimal('500.02'), realized_net_pnl_usd=Decimal(100))
    plan = allocate_profit(current, policy=ProfitWaterfallPolicy(minimum_profit_to_allocate_usd=Decimal(0)))
    assert plan.profit_above_high_water_usd == Decimal('0.02')
    assert all(value >= 0 for value in plan.allocations.values())
    assert sum(plan.allocations.values()) == Decimal('0.02')


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_snapshots_never_enter_the_ledger(tmp_path, value):
    ledger = EconomicLedger(str(tmp_path / 'noema.db'))
    with pytest.raises(ValueError, match='finite'):
        ledger.append_snapshot(replace(bootstrap_economy(Decimal(500)),
                                       realized_net_pnl_usd=Decimal(value)))
    assert ledger.latest_snapshot() is None


def test_zero_plan_cannot_hide_negative_allocations():
    plan = ProfitAllocation(Decimal(0), {
        CapitalBucket.RESERVE: Decimal(10), CapitalBucket.STRATEGY: Decimal(-10),
    })
    with pytest.raises(ValueError):
        apply_profit_plan(bootstrap_economy(Decimal(500)), plan)


def test_missing_calibration_cannot_earn_capital_authority():
    evidence = AutonomyEvidence(1500, 90, Decimal('.05'), Decimal(500), Decimal('.04'),
                                None, Decimal('.999'), 50, 20)
    assert earned_autonomy(evidence).earned_level == AutonomyLevel.DEMO


def test_failed_calls_consume_tokens_and_market_cooldown(tmp_path):
    store = CognitionStore(str(tmp_path / 'noema.db'))
    now = datetime(2026, 9, 27, tzinfo=UTC)
    kwargs = {'daily_limit_usd': 1, 'estimated_tokens': 600, 'hourly_token_limit': 1000,
              'now': now, 'market_id': 'A', 'cooldown_seconds': 300}
    assert store.reserve_estimated_cost(.1, **kwargs)
    # No successful packet was recorded: failure is still budgeted.
    assert not store.reserve_estimated_cost(.1, **kwargs)
    assert not store.reserve_estimated_cost(.1, **{**kwargs, 'market_id': 'B'})
    assert store.reserve_estimated_cost(.1, **{
        **kwargs, 'now': now + timedelta(hours=1, seconds=1),
    })


def test_mixed_timezone_and_legacy_unknown_tokens_fail_closed(tmp_path):
    store = CognitionStore(str(tmp_path / 'noema.db'))
    now = datetime(2026, 9, 27, tzinfo=UTC)
    assert store.reserve_estimated_cost(.1, daily_limit_usd=1, now=now)
    assert not store.reserve_estimated_cost(
        .1, daily_limit_usd=1, estimated_tokens=1, hourly_token_limit=1000,
        now=now.astimezone(timezone(timedelta(hours=-4))),
    )


def test_concurrent_reservations_cannot_overspend(tmp_path):
    path = str(tmp_path / 'noema.db')
    CognitionStore(path).conn.close()
    now = datetime(2026, 9, 27, tzinfo=UTC)

    def reserve(_):
        store = CognitionStore(path)
        try:
            return store.reserve_estimated_cost(.6, daily_limit_usd=1,
                                               estimated_tokens=600, hourly_token_limit=1000,
                                               now=now)
        finally:
            store.conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
