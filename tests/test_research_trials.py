from noema.research_trials import ResearchTrialStore


def test_trial_registry_counts_unique_parameter_searches(tmp_path) -> None:
    store = ResearchTrialStore(str(tmp_path / "trials.db"))

    first = store.register(
        family="trench-1",
        hypothesis="buyer acceleration predicts right-tail outcomes",
        params={"buyer_growth_min": 1.5, "liquidity_floor": 5000},
        feature_set_version="trench-v1",
    )
    duplicate = store.register(
        family="trench-1",
        hypothesis="buyer acceleration predicts right-tail outcomes",
        params={"liquidity_floor": 5000, "buyer_growth_min": 1.5},
        feature_set_version="trench-v1",
    )
    second = store.register(
        family="trench-1",
        hypothesis="buyer acceleration predicts right-tail outcomes",
        params={"buyer_growth_min": 2.0, "liquidity_floor": 5000},
        feature_set_version="trench-v1",
    )

    assert first == duplicate
    assert first != second
    assert store.count_family("trench-1") == 2

    store.set_status(first, "rejected")
    assert store.get(first).status == "rejected"
