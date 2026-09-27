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
    if snapshot.realized_profit_high_water_usd is not None:
        values["realized_profit_high_water_usd"] = snapshot.realized_profit_high_water_usd
    if any(not value.is_finite() for value in values.values()) or not (
        snapshot.realized_net_pnl_usd.is_finite()
    ):
        raise ValueError("economic snapshot values must be finite")
    if any(value < 0 for value in values.values()):
        raise ValueError("economic snapshot values must be non-negative")

    earmarked = earmarked_equity(snapshot)
    if earmarked > snapshot.current_equity_usd:
        raise ValueError("economic earmarks exceed current equity")


def earmarked_equity(snapshot: EconomicSnapshot) -> Decimal:
    return (
        snapshot.reserve_usd
        + snapshot.strategy_capital_usd
        + snapshot.research_budget_usd
        + snapshot.infrastructure_budget_usd
        + snapshot.treasury_sweep_usd
    )


def realized_profit_high_water(snapshot: EconomicSnapshot) -> Decimal:
    if snapshot.realized_profit_high_water_usd is not None:
        return snapshot.realized_profit_high_water_usd
    # Old snapshots have no realized watermark. Do not reallocate the profit
    # already represented by their equity watermark.
    return max(Decimal(0), snapshot.high_water_equity_usd - snapshot.starting_capital_usd)


def allocatable_realized_profit(snapshot: EconomicSnapshot) -> Decimal:
    """Internal planning limit, not proof of reconciled cash or profitability."""
    validate_snapshot(snapshot)
    return max(Decimal(0), min(
        snapshot.current_equity_usd - snapshot.high_water_equity_usd,
        snapshot.realized_net_pnl_usd - realized_profit_high_water(snapshot),
        snapshot.current_equity_usd - earmarked_equity(snapshot),
    ))


def apply_profit_plan(
    snapshot: EconomicSnapshot,
    plan: ProfitAllocation,
) -> EconomicSnapshot:
    validate_snapshot(snapshot)
    if (not plan.profit_above_high_water_usd.is_finite()
            or plan.profit_above_high_water_usd < 0):
        raise ValueError("profit plan amount must be finite and non-negative")
    for bucket, amount in plan.allocations.items():
        if not isinstance(bucket, CapitalBucket):
            raise TypeError("unknown profit allocation bucket")
        if not amount.is_finite() or amount < 0:
            raise ValueError("profit allocations must be finite and non-negative")

    allocated = sum(plan.allocations.values(), Decimal(0))
    if allocated != plan.profit_above_high_water_usd:
        raise ValueError("profit plan allocations do not sum to allocatable profit")
    if plan.profit_above_high_water_usd == 0:
        return snapshot
    if plan.profit_above_high_water_usd != allocatable_realized_profit(snapshot):
        raise ValueError("profit plan does not match snapshot allocatable realized profit")

    updated = replace(
        snapshot,
        # Advance only by the allocation: a remaining unrealized gain must not
        # prevent allocation when that gain is subsequently realized.
        high_water_equity_usd=(
            snapshot.high_water_equity_usd + plan.profit_above_high_water_usd
        ),
        realized_profit_high_water_usd=(
            realized_profit_high_water(snapshot) + plan.profit_above_high_water_usd
        ),
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
    validate_snapshot(snapshot)
    if bucket not in {CapitalBucket.RESEARCH, CapitalBucket.INFRASTRUCTURE}:
        raise ValueError("operating spend must use research or infrastructure bucket")
    if not amount_usd.is_finite() or amount_usd <= 0:
        raise ValueError("amount_usd must be positive and finite")

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
    validate_snapshot(snapshot)
    if not amount_usd.is_finite() or amount_usd <= 0:
        raise ValueError("amount_usd must be positive and finite")
    if amount_usd > snapshot.treasury_sweep_usd:
        raise ValueError("treasury sweep budget exceeded")

    updated = replace(
        snapshot,
        current_equity_usd=snapshot.current_equity_usd - amount_usd,
        treasury_sweep_usd=snapshot.treasury_sweep_usd - amount_usd,
    )
    validate_snapshot(updated)
    return updated
