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
