import pytest

from noema.trench_config import TrenchCollectorConfig


def test_trench_collector_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("NOEMA_TRENCH_ENABLED", raising=False)
    assert TrenchCollectorConfig.from_env().enabled is False


def test_trench_config_validates_limits() -> None:
    with pytest.raises(ValueError):
        TrenchCollectorConfig(due_limit=101).validate()
    with pytest.raises(ValueError):
        TrenchCollectorConfig(due_limit=2, enrichment_limit=3).validate()
