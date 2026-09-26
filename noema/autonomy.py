from __future__ import annotations

from decimal import Decimal

from .economic_models import (
    AutonomyDecision,
    AutonomyEvidence,
    AutonomyLevel,
)


_ORDER = (
    AutonomyLevel.SHADOW,
    AutonomyLevel.PAPER,
    AutonomyLevel.DEMO,
    AutonomyLevel.MICRO,
    AutonomyLevel.PROVEN,
    AutonomyLevel.SELF_FUNDED,
    AutonomyLevel.EXPANSION,
)


def _min_level(a: AutonomyLevel, b: AutonomyLevel) -> AutonomyLevel:
    return _ORDER[min(_ORDER.index(a), _ORDER.index(b))]


def earned_autonomy(
    evidence: AutonomyEvidence,
    *,
    current_level: AutonomyLevel = AutonomyLevel.SHADOW,
) -> AutonomyDecision:
    reasons: list[str] = []
    level = AutonomyLevel.SHADOW

    if evidence.resolved_forecasts >= 30:
        level = AutonomyLevel.PAPER
    else:
        reasons.append("fewer than 30 resolved forecasts")

    if (
        evidence.resolved_forecasts >= 100
        and evidence.live_observation_days >= 7
        and evidence.reconciliation_ok_fraction >= Decimal("0.99")
    ):
        level = AutonomyLevel.DEMO
    else:
        reasons.append("demo evidence gate incomplete")

    if (
        evidence.resolved_forecasts >= 250
        and evidence.live_observation_days >= 21
        and evidence.after_cost_return > 0
        and evidence.max_drawdown_fraction <= Decimal("0.05")
        and (
            evidence.calibration_error is None
            or evidence.calibration_error <= Decimal("0.08")
        )
        and evidence.reconciliation_ok_fraction >= Decimal("0.995")
    ):
        level = AutonomyLevel.MICRO
    else:
        reasons.append("micro-capital evidence gate incomplete")

    if (
        evidence.resolved_forecasts >= 1000
        and evidence.live_observation_days >= 60
        and evidence.realized_net_pnl_usd > 0
        and evidence.after_cost_return > Decimal("0.02")
        and evidence.max_drawdown_fraction <= Decimal("0.08")
        and (
            evidence.calibration_error is None
            or evidence.calibration_error <= Decimal("0.05")
        )
        and evidence.reconciliation_ok_fraction >= Decimal("0.999")
    ):
        level = AutonomyLevel.PROVEN
    else:
        reasons.append("proven-strategy evidence gate incomplete")

    if (
        level is AutonomyLevel.PROVEN
        and evidence.realized_net_pnl_usd >= Decimal("250")
        and evidence.profitable_days >= 30
        and evidence.losing_days <= evidence.profitable_days
    ):
        level = AutonomyLevel.SELF_FUNDED
    else:
        reasons.append("self-funded gate incomplete")

    if (
        level is AutonomyLevel.SELF_FUNDED
        and evidence.realized_net_pnl_usd >= Decimal("1000")
        and evidence.live_observation_days >= 180
        and evidence.max_drawdown_fraction <= Decimal("0.10")
    ):
        level = AutonomyLevel.EXPANSION
    else:
        reasons.append("expansion gate incomplete")

    # Never skip above what the evidence earns. Downshifts are immediate.
    earned = _min_level(level, level)
    return AutonomyDecision(
        current_level=current_level,
        earned_level=earned,
        reasons=tuple(reasons),
    )
