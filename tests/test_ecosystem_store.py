from noema.ecosystem import allocate_specialist_attention
from noema.ecosystem_store import EcosystemStore
from noema.specialists import SpecialistProfile, SpecialistState


def test_ecosystem_store_roundtrip_and_plan_dedup(tmp_path) -> None:
    store = EcosystemStore(str(tmp_path / "noema.db"))
    store.ensure_specialist(
        name="trench-1",
        family="solana_new_tokens",
        state=SpecialistState.SHADOW,
    )
    store.upsert_profile(
        SpecialistProfile(
            name="kalshi-history",
            family="prediction_markets",
            state=SpecialistState.PAPER,
            resolved=120,
            reliability=1.0,
            calibration_error=0.05,
            after_cost_return=0.01,
            drawdown_fraction=0.02,
        )
    )

    profiles = store.profiles()
    assert {profile.name for profile in profiles} == {"kalshi-history", "trench-1"}

    plan = allocate_specialist_attention(profiles)
    first = store.record_plan(plan)
    second = store.record_plan(plan)

    assert first == second
    latest = store.latest_plan()
    assert latest is not None
    assert latest["dominant_specialist"] == plan.dominant_specialist
