from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class CapitalBucket(str, Enum):
    RESERVE = "reserve"
    STRATEGY = "strategy"
    RESEARCH = "research"
    INFRASTRUCTURE = "infrastructure"
    TREASURY_SWEEP = "treasury_sweep"


class AutonomyLevel(str, Enum):
    SHADOW = "shadow"
    PAPER = "paper"
    DEMO = "demo"
    MICRO = "micro"
    PROVEN = "proven"
    SELF_FUNDED = "self_funded"
    EXPANSION = "expansion"


@dataclass(frozen=True)
class EconomicSnapshot:
    starting_capital_usd: Decimal
    current_equity_usd: Decimal
    realized_net_pnl_usd: Decimal
    high_water_equity_usd: Decimal
    reserve_usd: Decimal
    strategy_capital_usd: Decimal
    research_budget_usd: Decimal
    infrastructure_budget_usd: Decimal
    treasury_sweep_usd: Decimal


@dataclass(frozen=True)
class ProfitAllocation:
    profit_above_high_water_usd: Decimal
    allocations: dict[CapitalBucket, Decimal]


@dataclass(frozen=True)
class AutonomyEvidence:
    resolved_forecasts: int
    live_observation_days: int
    after_cost_return: Decimal
    realized_net_pnl_usd: Decimal
    max_drawdown_fraction: Decimal
    calibration_error: Decimal | None
    reconciliation_ok_fraction: Decimal
    profitable_days: int
    losing_days: int


@dataclass(frozen=True)
class AutonomyDecision:
    current_level: AutonomyLevel
    earned_level: AutonomyLevel
    reasons: tuple[str, ...]
