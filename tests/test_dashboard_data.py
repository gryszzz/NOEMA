from noema.dashboard_data import build_overview
from noema.soak import SoakStore


def test_overview_handles_empty_database(tmp_path) -> None:
    path = str(tmp_path / "noema.db")
    SoakStore(path)
    overview = build_overview(path)
    assert overview["database_present"] is True
    assert overview["forecasts"] == 0
    assert overview["resolved_markets"] == 0
