from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .economic_accounting import validate_snapshot
from .economic_models import EconomicSnapshot


@dataclass(frozen=True)
class BootstrapPolicy:
    reserve_fraction: Decimal = Decimal("0.50")
    strategy_fraction: Decimal = Decimal("0.40")
    research_fraction: Decimal = Decimal("0.10")


def bootstrap_economy(
    starting_capital_usd: Decimal,
    *,
    policy: BootstrapPolicy | None = None,
) -> EconomicSnapshot:
    policy = policy or BootstrapPolicy()
    if starting_capital_usd <= 0:
        raise ValueError("starting capital must be positive")

    fractions = (
        policy.reserve_fraction,
        policy.strategy_fraction,
        policy.research_fraction,
    )
    if any(fraction < 0 for fraction in fractions):
        raise ValueError("bootstrap fractions must be non-negative")
    if sum(fractions, Decimal(0)) != Decimal(1):
        raise ValueError("bootstrap fractions must sum to 1")

    snapshot = EconomicSnapshot(
        starting_capital_usd=starting_capital_usd,
        current_equity_usd=starting_capital_usd,
        realized_net_pnl_usd=Decimal(0),
        high_water_equity_usd=starting_capital_usd,
        reserve_usd=starting_capital_usd * policy.reserve_fraction,
        strategy_capital_usd=starting_capital_usd * policy.strategy_fraction,
        research_budget_usd=starting_capital_usd * policy.research_fraction,
        infrastructure_budget_usd=Decimal(0),
        treasury_sweep_usd=Decimal(0),
    )
    validate_snapshot(snapshot)
    return snapshot
