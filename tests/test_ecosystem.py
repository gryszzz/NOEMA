from noema.ecosystem import allocate_specialist_attention
from noema.specialists import SpecialistProfile, SpecialistState


def _profile(
    name: str,
    family: str,
    state: SpecialistState,
    *,
    resolved: int = 0,
    reliability: float = 0.0,
) -> SpecialistProfile:
    return SpecialistProfile(
        name=name,
        family=family,
        state=state,
        resolved=resolved,
        reliability=reliability,
        calibration_error=None,
        after_cost_return=None,
        drawdown_fraction=None,
    )


def test_shadow_specialist_gets_bounded_exploration() -> None:
    plan = allocate_specialist_attention(
        [
            _profile(
                "kalshi",
                "prediction",
                SpecialistState.PAPER,
                resolved=100,
                reliability=1.0,
            ),
            _profile("trench", "solana", SpecialistState.SHADOW),
        ],
        exploration_fraction=0.15,
        family_cap=1.0,
    )

    allocations = {item.specialist: item.attention_fraction for item in plan.allocations}
    assert allocations["trench"] == 0.15
    assert allocations["kalshi"] == 0.85
    assert plan.dominant_specialist == "kalshi"


def test_quarantined_specialist_receives_zero_attention() -> None:
    plan = allocate_specialist_attention(
        [
            _profile("bad", "solana", SpecialistState.QUARANTINED),
            _profile("new", "prediction", SpecialistState.SHADOW),
        ]
    )

    allocations = {item.specialist: item.attention_fraction for item in plan.allocations}
    assert allocations["bad"] == 0.0
    assert allocations["new"] > 0


def test_family_cap_leaves_excess_idle_instead_of_forcing_activity() -> None:
    plan = allocate_specialist_attention(
        [
            _profile(
                "a",
                "same-family",
                SpecialistState.PAPER,
                resolved=500,
                reliability=1.4,
            ),
            _profile(
                "b",
                "same-family",
                SpecialistState.ACTIVE_RESEARCH,
                resolved=500,
                reliability=1.4,
            ),
        ],
        family_cap=0.60,
    )

    allocated = sum(item.attention_fraction for item in plan.allocations)
    assert allocated <= 0.60 + 1e-9
    assert plan.idle_fraction >= 0.40 - 1e-9


def test_duplicate_specialist_name_is_rejected() -> None:
    profile = _profile("same", "one", SpecialistState.SHADOW)
    try:
        allocate_specialist_attention([profile, profile])
    except ValueError as exc:
        assert "duplicate specialist name" in str(exc)
    else:
        raise AssertionError("expected duplicate specialist rejection")
