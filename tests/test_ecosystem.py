import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from noema.ecosystem import allocate_specialist_attention
from noema.research_feedback import apply_research_feedback
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


def test_shadow_only_population_cannot_consume_exploitation_budget() -> None:
    plan = allocate_specialist_attention(
        [_profile("new", "prediction", SpecialistState.SHADOW)],
        exploration_fraction=0.15,
        family_cap=1.0,
    )
    assert plan.allocations[0].attention_fraction == pytest.approx(0.15)
    assert plan.idle_fraction == pytest.approx(0.85)


def test_zero_exploration_keeps_shadow_population_idle() -> None:
    plan = allocate_specialist_attention(
        [_profile("new", "prediction", SpecialistState.SHADOW)],
        exploration_fraction=0,
    )
    assert plan.allocations[0].attention_fraction == 0
    assert plan.idle_fraction == 1
    assert plan.dominant_specialist is None


def test_negative_paper_return_reduces_attention_even_without_competitors() -> None:
    profile = _profile("paper", "prediction", SpecialistState.PAPER)
    initial = allocate_specialist_attention([profile])
    losing = allocate_specialist_attention([replace(profile, after_cost_return=-0.10)])
    assert initial.allocations[0].attention_fraction == pytest.approx(0.65)
    assert losing.allocations[0].attention_fraction == pytest.approx(0.325)
    assert losing.idle_fraction > initial.idle_fraction
    assert "negative after-cost research evidence" in losing.allocations[0].reason


def test_losing_allocation_is_not_transferred_to_another_specialist() -> None:
    profiles = [
        _profile("a", "prediction", SpecialistState.PAPER),
        _profile("b", "web3", SpecialistState.PAPER),
    ]
    initial = allocate_specialist_attention(profiles)
    losing = allocate_specialist_attention([
        replace(profiles[0], after_cost_return=-0.20), profiles[1],
    ])
    shares = {item.specialist: item.attention_fraction for item in losing.allocations}
    assert shares["a"] < initial.allocations[0].attention_fraction
    assert shares["b"] == initial.allocations[1].attention_fraction
    assert losing.idle_fraction > initial.idle_fraction


@pytest.mark.parametrize("field", [
    "reliability", "calibration_error", "after_cost_return", "drawdown_fraction",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_evidence_cannot_receive_an_allocation(field, value) -> None:
    profile = replace(
        _profile("new", "prediction", SpecialistState.PAPER), **{field: value},
    )
    with pytest.raises(ValueError, match="finite"):
        allocate_specialist_attention([profile])


@pytest.mark.parametrize("field", [
    "exploration_fraction", "family_cap", "negative_return_scale",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_policy_is_rejected(field, value) -> None:
    with pytest.raises(ValueError):
        allocate_specialist_attention([], **{field: value})


def test_critic_rejection_reduces_attention_but_acceptance_does_not_reward_activity(tmp_path) -> None:
    profile = _profile("researcher", "prediction", SpecialistState.PAPER)
    plan = allocate_specialist_attention([profile])
    path = tmp_path / "feedback.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE autonomous_research_runs "
                     "(id INTEGER PRIMARY KEY,specialist,kind,status,result_json,created_at)")
        created = datetime.now(UTC).isoformat()
        conn.executemany("INSERT INTO autonomous_research_runs VALUES(?,?,?,?,?,?)", [
            (1, "researcher", "bounded_research", "completed",
             json.dumps({"critic_review": {"verdict": "PASS", "result_accepted": True}}), created),
            (2, "researcher", "bounded_research", "completed",
             json.dumps({"critic_review": {"verdict": "REJECT", "result_accepted": False}}),
             (datetime.now(UTC) + timedelta(microseconds=1)).isoformat()),
        ])
    result = apply_research_feedback(str(path), plan)
    assert result.allocations[0].attention_fraction == pytest.approx(
        plan.allocations[0].attention_fraction * 0.5,
    )
    assert result.idle_fraction > plan.idle_fraction
    assert "critic rejection" in result.allocations[0].reason
