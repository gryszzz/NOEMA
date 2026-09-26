from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class LossResponse:
    risk_multiplier: Decimal
    research_only: bool
    reasons: tuple[str, ...]


def loss_response(
    *,
    drawdown_fraction: Decimal,
    daily_loss_fraction: Decimal,
    consecutive_losing_days: int,
) -> LossResponse:
    if drawdown_fraction < 0 or daily_loss_fraction < 0:
        raise ValueError("loss fractions must be non-negative")
    if consecutive_losing_days < 0:
        raise ValueError("consecutive_losing_days must be non-negative")

    reasons: list[str] = []
    multiplier = Decimal(1)
    research_only = False

    if drawdown_fraction >= Decimal("0.10"):
        multiplier = Decimal(0)
        research_only = True
        reasons.append("10% drawdown: research-only quarantine")
    elif drawdown_fraction >= Decimal("0.07"):
        multiplier = min(multiplier, Decimal("0.25"))
        reasons.append("7% drawdown: risk quartered")
    elif drawdown_fraction >= Decimal("0.04"):
        multiplier = min(multiplier, Decimal("0.50"))
        reasons.append("4% drawdown: risk halved")

    if daily_loss_fraction >= Decimal("0.03"):
        multiplier = Decimal(0)
        research_only = True
        reasons.append("3% daily loss: halt risk for the day")
    elif daily_loss_fraction >= Decimal("0.015"):
        multiplier = min(multiplier, Decimal("0.50"))
        reasons.append("1.5% daily loss: risk halved")

    if consecutive_losing_days >= 5:
        multiplier = Decimal(0)
        research_only = True
        reasons.append("five losing days: research-only quarantine")
    elif consecutive_losing_days >= 3:
        multiplier = min(multiplier, Decimal("0.50"))
        reasons.append("three losing days: risk halved")

    return LossResponse(multiplier, research_only, tuple(reasons))
