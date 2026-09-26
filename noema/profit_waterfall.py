from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .economic_models import CapitalBucket, EconomicSnapshot, ProfitAllocation


@dataclass(frozen=True)
class ProfitWaterfallPolicy:
    reserve_fraction: Decimal = Decimal("0.35")
    strategy_fraction: Decimal = Decimal("0.30")
    research_fraction: Decimal = Decimal("0.15")
    infrastructure_fraction: Decimal = Decimal("0.10")
    treasury_sweep_fraction: Decimal = Decimal("0.10")
    minimum_profit_to_allocate_usd: Decimal = Decimal(1)


def _validate_policy(policy: ProfitWaterfallPolicy) -> None:
    values = (
        policy.reserve_fraction,
        policy.strategy_fraction,
        policy.research_fraction,
        policy.infrastructure_fraction,
        policy.treasury_sweep_fraction,
    )
    if any(value < 0 for value in values):
        raise ValueError("profit fractions must be non-negative")
    if sum(values, Decimal(0)) != Decimal(1):
        raise ValueError("profit fractions must sum to 1")
    if policy.minimum_profit_to_allocate_usd < 0:
        raise ValueError("minimum profit must be non-negative")


def allocate_profit(
    snapshot: EconomicSnapshot,
    *,
    policy: ProfitWaterfallPolicy | None = None,
) -> ProfitAllocation:
    policy = policy or ProfitWaterfallPolicy()
    _validate_policy(policy)

    profit = max(
        Decimal(0),
        snapshot.current_equity_usd - snapshot.high_water_equity_usd,
    )
    if profit < policy.minimum_profit_to_allocate_usd:
        profit = Decimal(0)

    fractions = {
        CapitalBucket.RESERVE: policy.reserve_fraction,
        CapitalBucket.STRATEGY: policy.strategy_fraction,
        CapitalBucket.RESEARCH: policy.research_fraction,
        CapitalBucket.INFRASTRUCTURE: policy.infrastructure_fraction,
        CapitalBucket.TREASURY_SWEEP: policy.treasury_sweep_fraction,
    }
    buckets = list(fractions)
    allocations: dict[CapitalBucket, Decimal] = {}
    assigned = Decimal(0)
    for bucket in buckets[:-1]:
        amount = (profit * fractions[bucket]).quantize(Decimal("0.01"))
        allocations[bucket] = amount
        assigned += amount
    allocations[buckets[-1]] = profit - assigned

    return ProfitAllocation(
        profit_above_high_water_usd=profit,
        allocations=allocations,
    )
