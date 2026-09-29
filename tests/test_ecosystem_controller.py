from noema.ecosystem_controller import review_research_ecosystem
from noema.ecosystem_store import EcosystemStore


def test_ecosystem_controller_bootstraps_default_specialists(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    plan = review_research_ecosystem(db)

    names = {item.specialist for item in plan.allocations}
    assert {"kalshi-history", "trench-1"} <= names
    assert plan.dominant_specialist == "kalshi-history"

    store = EcosystemStore(db)
    assert store.latest_plan() is not None


def test_mission_allocation_review_reuses_existing_audit_and_is_idempotent(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    plan = review_research_ecosystem(db)
    review = {
        "mission_id": "mission-fixture", "specialist": "kalshi-history",
        "outcome": "NO_CHANGE", "basis": "No new after-cost evidence supports a change.",
        "previous_attention_fraction": .65, "current_attention_fraction": .65,
    }
    store = EcosystemStore(db)
    review_id = store.record_mission_review(plan, review)
    assert store.record_mission_review(plan, review) == review_id
    assert store.latest_plan()["mission_review"]["outcome"] == "NO_CHANGE"
    store.conn.close()

    review_research_ecosystem(db)
    store = EcosystemStore(db)
    assert store.conn.execute("SELECT COUNT(*) FROM ecosystem_reviews").fetchone()[0] == 2
    assert store.latest_plan()["mission_review"]["mission_id"] == "mission-fixture"
    store.conn.close()
