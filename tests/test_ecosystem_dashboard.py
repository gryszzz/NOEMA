from noema.ecosystem_controller import ensure_default_specialists
from noema.ecosystem_dashboard import build_ecosystem_overview
from noema.ecosystem_evolution import evolve_default_specialists


def test_ecosystem_overview_exposes_profiles_reviews_and_trials(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    ensure_default_specialists(db)
    evolve_default_specialists(db)

    overview = build_ecosystem_overview(db)

    assert overview["database_present"] is True
    names = {row["name"] for row in overview["specialists"]}
    assert {"kalshi-history", "trench-1"} <= names
    assert overview["latest_plan"] is None
