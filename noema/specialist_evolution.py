from __future__ import annotations

from dataclasses import dataclass, replace

from .specialists import SpecialistProfile, SpecialistState


@dataclass(frozen=True)
class SpecialistEvidence:
    resolved: int
    brier: float | None
    market_baseline_brier: float | None
    after_cost_return: float | None
    max_drawdown_fraction: float | None
    calibration_error: float | None
    research_credible: bool | None
    probability_backtest_overfit: float | None = None
    probabilistic_sharpe: float | None = None

    def __post_init__(self) -> None:
        if self.resolved < 0:
            raise ValueError("resolved must be non-negative")
        for name in ("brier", "market_baseline_brier"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in (
            "max_drawdown_fraction",
            "calibration_error",
            "probability_backtest_overfit",
            "probabilistic_sharpe",
        ):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True)
class EvolutionPolicy:
    paper_min_resolved: int = 30
    active_min_resolved: int = 100
    promote_reviews: int = 2
    downshift_reviews: int = 2
    recovery_reviews: int = 3
    max_active_calibration_error: float = 0.10
    hard_calibration_error: float = 0.20
    max_active_drawdown: float = 0.10
    hard_drawdown: float = 0.20
    max_pbo: float = 0.50
    hard_pbo: float = 0.80
    min_psr: float = 0.80
    min_brier_improvement_fraction: float = 0.01


@dataclass(frozen=True)
class EvolutionDecision:
    previous_state: SpecialistState
    next_state: SpecialistState
    reliability: float
    success_streak: int
    failure_streak: int
    promoted: bool
    demoted: bool
    hard_quarantine: bool
    reasons: tuple[str, ...]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _brier_improvement_fraction(evidence: SpecialistEvidence) -> float | None:
    if (
        evidence.brier is None
        or evidence.market_baseline_brier is None
        or evidence.market_baseline_brier <= 0
    ):
        return None
    return (
        evidence.market_baseline_brier - evidence.brier
    ) / evidence.market_baseline_brier


def evidence_reliability(evidence: SpecialistEvidence) -> float:
    """Map heterogeneous evidence into a bounded research-reliability score.

    The score is intentionally conservative and cannot itself authorize capital.
    """

    sample = _clamp(evidence.resolved / 500.0)

    improvement = _brier_improvement_fraction(evidence)
    predictive = 0.25 if improvement is None else _clamp(0.5 + improvement * 5)

    calibration = (
        0.35
        if evidence.calibration_error is None
        else 1.0 - _clamp(evidence.calibration_error / 0.20)
    )

    if evidence.after_cost_return is None:
        economic = 0.25
    elif evidence.after_cost_return <= 0:
        economic = 0.0
    else:
        economic = _clamp(evidence.after_cost_return / 0.10)

    drawdown = (
        0.50
        if evidence.max_drawdown_fraction is None
        else 1.0 - _clamp(evidence.max_drawdown_fraction / 0.20)
    )

    credibility = (
        0.50
        if evidence.research_credible is None
        else float(evidence.research_credible)
    )

    overfit = (
        0.50
        if evidence.probability_backtest_overfit is None
        else 1.0 - evidence.probability_backtest_overfit
    )
    psr = (
        0.50
        if evidence.probabilistic_sharpe is None
        else evidence.probabilistic_sharpe
    )

    score = (
        0.18 * sample
        + 0.18 * predictive
        + 0.15 * calibration
        + 0.15 * economic
        + 0.10 * drawdown
        + 0.10 * credibility
        + 0.07 * overfit
        + 0.07 * psr
    )
    return _clamp(score * 1.5, high=1.5)


def _hard_failures(
    evidence: SpecialistEvidence,
    policy: EvolutionPolicy,
) -> list[str]:
    reasons: list[str] = []
    if evidence.research_credible is False:
        reasons.append("research credibility failed")
    if (
        evidence.max_drawdown_fraction is not None
        and evidence.max_drawdown_fraction >= policy.hard_drawdown
    ):
        reasons.append("hard drawdown limit breached")
    if (
        evidence.calibration_error is not None
        and evidence.calibration_error >= policy.hard_calibration_error
    ):
        reasons.append("hard calibration limit breached")
    if (
        evidence.probability_backtest_overfit is not None
        and evidence.probability_backtest_overfit >= policy.hard_pbo
    ):
        reasons.append("backtest-overfit probability too high")
    return reasons


def _active_quality_failures(
    evidence: SpecialistEvidence,
    policy: EvolutionPolicy,
) -> list[str]:
    reasons: list[str] = []
    improvement = _brier_improvement_fraction(evidence)

    if evidence.resolved < policy.active_min_resolved:
        reasons.append("active sample gate incomplete")
    if evidence.research_credible is not True:
        reasons.append("research credibility not established")
    if (
        improvement is None
        or improvement < policy.min_brier_improvement_fraction
    ):
        reasons.append("predictive improvement gate incomplete")
    if evidence.after_cost_return is None or evidence.after_cost_return <= 0:
        reasons.append("after-cost return not positive")
    if (
        evidence.max_drawdown_fraction is None
        or evidence.max_drawdown_fraction > policy.max_active_drawdown
    ):
        reasons.append("drawdown gate incomplete")
    if (
        evidence.calibration_error is None
        or evidence.calibration_error > policy.max_active_calibration_error
    ):
        reasons.append("calibration gate incomplete")
    if (
        evidence.probability_backtest_overfit is not None
        and evidence.probability_backtest_overfit > policy.max_pbo
    ):
        reasons.append("overfit diagnostic above active threshold")
    if (
        evidence.probabilistic_sharpe is not None
        and evidence.probabilistic_sharpe < policy.min_psr
    ):
        reasons.append("probabilistic Sharpe below active threshold")
    return reasons


def evolve_specialist(
    profile: SpecialistProfile,
    evidence: SpecialistEvidence,
    *,
    success_streak: int = 0,
    failure_streak: int = 0,
    policy: EvolutionPolicy | None = None,
) -> EvolutionDecision:
    """Update specialist maturity with immediate hard failure and slow promotion.

    Shadow -> Paper is a maturity transition.
    Paper -> Active Research requires repeated high-quality reviews.
    Active -> Paper requires repeated soft failures.
    Any state -> Quarantined is immediate on a hard failure.
    Quarantined -> Paper requires repeated clean active-quality reviews.
    """

    policy = policy or EvolutionPolicy()
    if success_streak < 0 or failure_streak < 0:
        raise ValueError("review streaks must be non-negative")

    reliability = evidence_reliability(evidence)
    hard_reasons = _hard_failures(evidence, policy)
    if hard_reasons:
        return EvolutionDecision(
            previous_state=profile.state,
            next_state=SpecialistState.QUARANTINED,
            reliability=reliability,
            success_streak=0,
            failure_streak=failure_streak + 1,
            promoted=False,
            demoted=profile.state is not SpecialistState.QUARANTINED,
            hard_quarantine=True,
            reasons=tuple(hard_reasons),
        )

    active_failures = _active_quality_failures(evidence, policy)
    active_quality = not active_failures

    if active_quality:
        success_streak += 1
        failure_streak = 0
    else:
        failure_streak += 1
        success_streak = 0

    next_state = profile.state
    reasons: list[str] = []

    if profile.state is SpecialistState.QUARANTINED:
        if active_quality and success_streak >= policy.recovery_reviews:
            next_state = SpecialistState.PAPER
            reasons.append("quarantine recovery gate passed; return to paper")
        else:
            reasons.extend(active_failures or ["quarantine recovery review incomplete"])

    elif profile.state is SpecialistState.SHADOW:
        if evidence.resolved >= policy.paper_min_resolved:
            next_state = SpecialistState.PAPER
            reasons.append("minimum forward evidence reached; promote to paper")
        else:
            reasons.append("collecting shadow evidence")

    elif profile.state is SpecialistState.PAPER:
        if active_quality and success_streak >= policy.promote_reviews:
            next_state = SpecialistState.ACTIVE_RESEARCH
            reasons.append("repeated active-quality reviews passed")
        else:
            reasons.extend(active_failures or ["promotion streak incomplete"])

    elif profile.state is SpecialistState.ACTIVE_RESEARCH:
        if active_quality:
            reasons.append("active-quality evidence maintained")
        elif failure_streak >= policy.downshift_reviews:
            next_state = SpecialistState.PAPER
            reasons.extend(active_failures)
            reasons.append("repeated soft failures; downshift to paper")
        else:
            reasons.extend(active_failures)
            reasons.append("soft failure recorded; waiting for hysteresis")

    return EvolutionDecision(
        previous_state=profile.state,
        next_state=next_state,
        reliability=reliability,
        success_streak=success_streak,
        failure_streak=failure_streak,
        promoted=next_state is not profile.state
        and next_state in {SpecialistState.PAPER, SpecialistState.ACTIVE_RESEARCH},
        demoted=next_state is not profile.state
        and next_state in {SpecialistState.PAPER, SpecialistState.QUARANTINED},
        hard_quarantine=False,
        reasons=tuple(reasons),
    )


def apply_evolution(
    profile: SpecialistProfile,
    evidence: SpecialistEvidence,
    decision: EvolutionDecision,
) -> SpecialistProfile:
    return replace(
        profile,
        state=decision.next_state,
        resolved=evidence.resolved,
        reliability=decision.reliability,
        calibration_error=evidence.calibration_error,
        after_cost_return=evidence.after_cost_return,
        drawdown_fraction=evidence.max_drawdown_fraction,
    )
