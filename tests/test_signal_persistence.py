from datetime import UTC, datetime, timedelta

from noema.signal_persistence import PersistenceState


def test_edge_must_persist_above_threshold() -> None:
    start = datetime.now(UTC)
    state = PersistenceState(threshold=0.03)
    state.observe(0.04, observed_at=start)
    state.observe(0.05, observed_at=start + timedelta(seconds=1))

    assert state.observations == 2
    assert state.age_ms(now=start + timedelta(seconds=1)) == 1000


def test_edge_drop_resets_persistence() -> None:
    state = PersistenceState(threshold=0.03)
    state.observe(0.04)
    state.observe(0.01)
    assert state.first_seen_at is None
    assert state.observations == 0
