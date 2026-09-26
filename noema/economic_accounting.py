from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .economic_models import CapitalBucket, EconomicSnapshot, ProfitAllocation


def validate_snapshot(snapshot: EconomicSnapshot) -> None:
    values = {
        "starting_capital_usd": snapshot.starting_capital_usd,
        "current_equity_usd": snapshot.current_equity_usd,
        "high_water_equity_usd": snapshot.high_water_equity_usd,
        "reserve_usd": snapshot.reserve_usd,
        "strategy_capital_usd": snapshot.strategy_capital_usd,
        "research_budget_usd": snapshot.research_budget_usd,
        "infrastructure_budget_usd": snapshot.infrastructure_budget_usd,
        "treasury_sweep_usd": snapshot.treasury_sweep_usd,
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("economic snapshot values must be non-negative")

    earmarked = (
        snapshot.reserve_usd
        + snapshot.strategy_capital_usd
        + snapshot.research_budget_usd
        + snapshot.infrastructure_budget_usd
        + snapshot.treasury_sweep_usd
    )
    if earmarked > snapshot.current_equity_usd:
        raise ValueError("economic earmarks exceed current equity")


def apply_profit_plan(
    snapshot: EconomicSnapshot,
    plan: ProfitAllocation,
) -> EconomicSnapshot:
    validate_snapshot(snapshot)

    expected = max(
        Decimal(0),
        snapshot.current_equity_usd - snapshot.high_water_equity_usd,
    )
    if plan.profit_above_high_water_usd != expected:
        raise ValueError("profit plan does not match snapshot high-water profit")

    allocated = sum(plan.allocations.values(), Decimal(0))
    if allocated != plan.profit_above_high_water_usd:
        raise ValueError("profit plan allocations do not sum to allocatable profit")

    updated = replace(
        snapshot,
        high_water_equity_usd=snapshot.current_equity_usd,
        reserve_usd=snapshot.reserve_usd
        + plan.allocations.get(CapitalBucket.RESERVE, Decimal(0)),
        strategy_capital_usd=snapshot.strategy_capital_usd
        + plan.allocations.get(CapitalBucket.STRATEGY, Decimal(0)),
        research_budget_usd=snapshot.research_budget_usd
        + plan.allocations.get(CapitalBucket.RESEARCH, Decimal(0)),
        infrastructure_budget_usd=snapshot.infrastructure_budget_usd
        + plan.allocations.get(CapitalBucket.INFRASTRUCTURE, Decimal(0)),
        treasury_sweep_usd=snapshot.treasury_sweep_usd
        + plan.allocations.get(CapitalBucket.TREASURY_SWEEP, Decimal(0)),
    )
    validate_snapshot(updated)
    return updated


def spend_operating_budget(
    snapshot: EconomicSnapshot,
    *,
    bucket: CapitalBucket,
    amount_usd: Decimal,
) -> EconomicSnapshot:
    if bucket not in {CapitalBucket.RESEARCH, CapitalBucket.INFRASTRUCTURE}:
        raise ValueError("operating spend must use research or infrastructure bucket")
    if amount_usd <= 0:
        raise ValueError("amount_usd must be positive")

    available = (
        snapshot.research_budget_usd
        if bucket is CapitalBucket.RESEARCH
        else snapshot.infrastructure_budget_usd
    )
    if amount_usd > available:
        raise ValueError("operating budget exceeded")

    updates = {
        "current_equity_usd": snapshot.current_equity_usd - amount_usd,
        "realized_net_pnl_usd": snapshot.realized_net_pnl_usd - amount_usd,
    }
    if bucket is CapitalBucket.RESEARCH:
        updates["research_budget_usd"] = snapshot.research_budget_usd - amount_usd
    else:
        updates["infrastructure_budget_usd"] = (
            snapshot.infrastructure_budget_usd - amount_usd
        )

    updated = replace(snapshot, **updates)
    validate_snapshot(updated)
    return updated


def execute_treasury_sweep(
    snapshot: EconomicSnapshot,
    amount_usd: Decimal,
) -> EconomicSnapshot:
    if amount_usd <= 0:
        raise ValueError("amount_usd must be positive")
    if amount_usd > snapshot.treasury_sweep_usd:
        raise ValueError("treasury sweep budget exceeded")

    updated = replace(
        snapshot,
        current_equity_usd=snapshot.current_equity_usd - amount_usd,
        treasury_sweep_usd=snapshot.treasury_sweep_usd - amount_usd,
    )
    validate_snapshot(updated)
    return updated
