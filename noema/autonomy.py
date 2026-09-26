from __future__ import annotations

from decimal import Decimal

from .economic_models import AutonomyDecision, AutonomyEvidence, AutonomyLevel


def earned_autonomy(
    evidence: AutonomyEvidence,
    *,
    current_level: AutonomyLevel = AutonomyLevel.SHADOW,
) -> AutonomyDecision:
    reasons: list[str] = []
    level = AutonomyLevel.SHADOW

    if evidence.resolved_forecasts < 30:
        reasons.append("shadow: fewer than 30 resolved forecasts")
        return AutonomyDecision(current_level, level, tuple(reasons))
    level = AutonomyLevel.PAPER

    demo_ok = (
        evidence.resolved_forecasts >= 100
        and evidence.live_observation_days >= 7
        and evidence.reconciliation_ok_fraction >= Decimal("0.99")
    )
    if not demo_ok:
        reasons.append("demo gate incomplete")
        return AutonomyDecision(current_level, level, tuple(reasons))
    level = AutonomyLevel.DEMO

    micro_ok = (
        evidence.resolved_forecasts >= 250
        and evidence.live_observation_days >= 21
        and evidence.after_cost_return > 0
        and evidence.max_drawdown_fraction <= Decimal("0.05")
        and (
            evidence.calibration_error is None
            or evidence.calibration_error <= Decimal("0.08")
        )
        and evidence.reconciliation_ok_fraction >= Decimal("0.995")
    )
    if not micro_ok:
        reasons.append("micro-capital gate incomplete")
        return AutonomyDecision(current_level, level, tuple(reasons))
    level = AutonomyLevel.MICRO

    proven_ok = (
        evidence.resolved_forecasts >= 1000
        and evidence.live_observation_days >= 60
        and evidence.realized_net_pnl_usd > 0
        and evidence.after_cost_return > Decimal("0.02")
        and evidence.max_drawdown_fraction <= Decimal("0.05")
        and (
            evidence.calibration_error is None
            or evidence.calibration_error <= Decimal("0.05")
        )
        and evidence.reconciliation_ok_fraction >= Decimal("0.999")
    )
    if not proven_ok:
        reasons.append("proven-strategy gate incomplete")
        return AutonomyDecision(current_level, level, tuple(reasons))
    level = AutonomyLevel.PROVEN

    self_funded_ok = (
        evidence.realized_net_pnl_usd >= Decimal(250)
        and evidence.profitable_days >= 30
        and evidence.losing_days <= evidence.profitable_days
    )
    if not self_funded_ok:
        reasons.append("self-funded gate incomplete")
        return AutonomyDecision(current_level, level, tuple(reasons))
    level = AutonomyLevel.SELF_FUNDED

    expansion_ok = (
        evidence.realized_net_pnl_usd >= Decimal(1000)
        and evidence.live_observation_days >= 180
        and evidence.max_drawdown_fraction <= Decimal("0.05")
        and evidence.profitable_days >= 90
    )
    if not expansion_ok:
        reasons.append("expansion gate incomplete")
        return AutonomyDecision(current_level, level, tuple(reasons))

    return AutonomyDecision(
        current_level=current_level,
        earned_level=AutonomyLevel.EXPANSION,
        reasons=(),
    )
