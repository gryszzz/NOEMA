from noema.adaptive_trust import AdaptiveTrustStore, TrustState, update_trust


def test_model_beating_market_gains_trust() -> None:
    state = TrustState("m", 0, 0.0, 0.0, 0.0)
    for _ in range(100):
        state = update_trust(
            state,
            model_probability=0.80,
            market_probability=0.55,
            outcome_yes=1,
        )
    assert state.log_weight > 0
    assert state.reliability > 0.5


def test_trust_store_roundtrip(tmp_path) -> None:
    store = AdaptiveTrustStore(str(tmp_path / "noema.db"))
    state = TrustState("m", 5, 0.2, 1.0, 1.2)
    store.put(state)
    assert store.get("m") == state
