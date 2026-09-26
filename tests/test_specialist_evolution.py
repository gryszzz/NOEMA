from noema.specialist_evolution import (
    SpecialistEvidence,
    evolve_specialist,
)
from noema.specialists import SpecialistProfile, SpecialistState


def profile(state: SpecialistState) -> SpecialistProfile:
    return SpecialistProfile(
        name="test",
        family="test-family",
        state=state,
        resolved=0,
        reliability=0.0,
        calibration_error=None,
        after_cost_return=None,
        drawdown_fraction=None,
    )


def strong_evidence(resolved: int = 150) -> SpecialistEvidence:
    return SpecialistEvidence(
        resolved=resolved,
        brier=0.18,
        market_baseline_brier=0.22,
        after_cost_return=0.04,
        max_drawdown_fraction=0.04,
        calibration_error=0.04,
        research_credible=True,
        probability_backtest_overfit=0.20,
        probabilistic_sharpe=0.90,
    )


def test_shadow_matures_to_paper_after_minimum_forward_sample() -> None:
    immature = evolve_specialist(
        profile(SpecialistState.SHADOW),
        strong_evidence(resolved=29),
    )
    assert immature.next_state is SpecialistState.SHADOW

    mature = evolve_specialist(
        profile(SpecialistState.SHADOW),
        strong_evidence(resolved=30),
    )
    assert mature.next_state is SpecialistState.PAPER
    assert mature.promoted is True


def test_paper_requires_repeated_strong_reviews_before_active() -> None:
    first = evolve_specialist(
        profile(SpecialistState.PAPER),
        strong_evidence(),
    )
    assert first.next_state is SpecialistState.PAPER
    assert first.success_streak == 1

    second = evolve_specialist(
        profile(SpecialistState.PAPER),
        strong_evidence(),
        success_streak=first.success_streak,
        failure_streak=first.failure_streak,
    )
    assert second.next_state is SpecialistState.ACTIVE_RESEARCH
    assert second.promoted is True


def test_active_soft_failure_uses_hysteresis() -> None:
    weak = SpecialistEvidence(
        resolved=150,
        brier=0.23,
        market_baseline_brier=0.22,
        after_cost_return=-0.01,
        max_drawdown_fraction=0.08,
        calibration_error=0.08,
        research_credible=None,
    )
    first = evolve_specialist(
        profile(SpecialistState.ACTIVE_RESEARCH),
        weak,
    )
    assert first.next_state is SpecialistState.ACTIVE_RESEARCH
    assert first.failure_streak == 1

    second = evolve_specialist(
        profile(SpecialistState.ACTIVE_RESEARCH),
        weak,
        success_streak=first.success_streak,
        failure_streak=first.failure_streak,
    )
    assert second.next_state is SpecialistState.PAPER
    assert second.demoted is True


def test_hard_failure_quarantines_immediately() -> None:
    failed = SpecialistEvidence(
        resolved=150,
        brier=0.18,
        market_baseline_brier=0.22,
        after_cost_return=0.04,
        max_drawdown_fraction=0.25,
        calibration_error=0.04,
        research_credible=True,
    )
    result = evolve_specialist(profile(SpecialistState.PAPER), failed)
    assert result.next_state is SpecialistState.QUARANTINED
    assert result.hard_quarantine is True
    assert result.demoted is True


def test_quarantine_requires_repeated_clean_reviews_to_recover() -> None:
    state = profile(SpecialistState.QUARANTINED)
    first = evolve_specialist(state, strong_evidence())
    second = evolve_specialist(
        state,
        strong_evidence(),
        success_streak=first.success_streak,
        failure_streak=first.failure_streak,
    )
    third = evolve_specialist(
        state,
        strong_evidence(),
        success_streak=second.success_streak,
        failure_streak=second.failure_streak,
    )

    assert first.next_state is SpecialistState.QUARANTINED
    assert second.next_state is SpecialistState.QUARANTINED
    assert third.next_state is SpecialistState.PAPER
    assert third.promoted is True
    assert third.demoted is False
