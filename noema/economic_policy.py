from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .economic_models import AutonomyLevel


@dataclass(frozen=True)
class LevelLimits:
    max_transaction_usd: Decimal
    max_daily_notional_usd: Decimal
    minimum_reserve_usd: Decimal
    research_budget_fraction: Decimal
    infrastructure_budget_fraction: Decimal


_LIMITS = {
    AutonomyLevel.SHADOW: LevelLimits(
        Decimal(0), Decimal(0), Decimal(0), Decimal("0.00"), Decimal("0.00")
    ),
    AutonomyLevel.PAPER: LevelLimits(
        Decimal(0), Decimal(0), Decimal(0), Decimal("0.05"), Decimal("0.00")
    ),
    AutonomyLevel.DEMO: LevelLimits(
        Decimal(0), Decimal(0), Decimal(0), Decimal("0.10"), Decimal("0.00")
    ),
    AutonomyLevel.MICRO: LevelLimits(
        Decimal(5), Decimal(20), Decimal(100), Decimal("0.10"), Decimal("0.00")
    ),
    AutonomyLevel.PROVEN: LevelLimits(
        Decimal(15), Decimal(75), Decimal(150), Decimal("0.12"), Decimal("0.03")
    ),
    AutonomyLevel.SELF_FUNDED: LevelLimits(
        Decimal(25), Decimal(150), Decimal(200), Decimal("0.15"), Decimal("0.05")
    ),
    AutonomyLevel.EXPANSION: LevelLimits(
        Decimal(50), Decimal(300), Decimal(300), Decimal("0.20"), Decimal("0.10")
    ),
}


def limits_for_level(level: AutonomyLevel) -> LevelLimits:
    return _LIMITS[level]
