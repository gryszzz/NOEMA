from noema.entry_quality import EntryInputs, assess_entry_quality


def test_stable_high_quality_entry_can_pass() -> None:
    result = assess_entry_quality(
        EntryInputs(
            after_cost_edge=0.08,
            spread=0.02,
            depth_imbalance=0.10,
            feed_latency_ms=50,
            edge_persistence_ms=2500,
            price_change_last_second=0.01,
        )
    )
    assert result.eligible is True
    assert result.score > 0.55


def test_one_tick_shock_is_rejected() -> None:
    result = assess_entry_quality(
        EntryInputs(
            after_cost_edge=0.10,
            spread=0.02,
            depth_imbalance=0.10,
            feed_latency_ms=50,
            edge_persistence_ms=100,
            price_change_last_second=0.10,
        )
    )
    assert result.eligible is False
    assert "price shock too large for stable entry" in result.reasons
