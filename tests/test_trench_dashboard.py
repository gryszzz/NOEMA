from noema.trench_dashboard import build_trench_overview


def test_trench_overview_is_safe_without_database(tmp_path) -> None:
    overview = build_trench_overview(str(tmp_path / "missing.db"))
    assert overview["database_present"] is False
    assert overview["counts"]["launches"] == 0
