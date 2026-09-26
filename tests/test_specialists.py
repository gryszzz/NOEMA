from noema.specialists import (
    SpecialistProfile,
    SpecialistState,
    specialist_attention_multiplier,
)


def test_quarantined_specialist_gets_no_attention() -> None:
    profile = SpecialistProfile(
        name="sports",
        family="sports",
        state=SpecialistState.QUARANTINED,
        resolved=500,
        reliability=1.4,
        calibration_error=0.02,
        after_cost_return=0.2,
        drawdown_fraction=0.03,
    )
    assert specialist_attention_multiplier(profile) == 0.0


def test_proven_specialist_gets_more_attention_than_shadow() -> None:
    proven = SpecialistProfile(
        name="weather",
        family="weather",
        state=SpecialistState.ACTIVE_RESEARCH,
        resolved=500,
        reliability=1.4,
        calibration_error=0.02,
        after_cost_return=0.1,
        drawdown_fraction=0.02,
    )
    shadow = SpecialistProfile(
        name="new",
        family="new",
        state=SpecialistState.SHADOW,
        resolved=0,
        reliability=0.0,
        calibration_error=None,
        after_cost_return=None,
        drawdown_fraction=None,
    )
    assert specialist_attention_multiplier(proven) > specialist_attention_multiplier(shadow)
