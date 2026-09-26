from noema.research_trials import ResearchTrialStore


def test_recent_research_trials_can_filter_family_and_status(tmp_path) -> None:
    store = ResearchTrialStore(str(tmp_path / "noema.db"))
    first = store.register(
        family="a",
        hypothesis="one",
        params={"x": 1},
        feature_set_version="v1",
    )
    store.register(
        family="b",
        hypothesis="two",
        params={"x": 2},
        feature_set_version="v1",
    )
    store.set_status(first, "running")

    rows = store.recent(status="running", family="a")

    assert len(rows) == 1
    assert rows[0].trial_id == first
