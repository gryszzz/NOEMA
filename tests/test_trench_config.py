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


def test_optional_rpc_fallback_is_loaded_without_echoing_credentials(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_SOLANA_RPC_URL", "https://primary.example/rpc?token=private-primary")
    monkeypatch.setenv("NOEMA_SOLANA_RPC_FALLBACK_URL", "https://fallback.example/rpc?token=private-fallback")
    monkeypatch.setenv("NOEMA_JUPITER_API_KEY", "private-jupiter")
    config = TrenchCollectorConfig.from_env()
    assert config.solana_rpc_url.endswith("private-primary")
    assert config.solana_rpc_fallback_url.endswith("private-fallback")
    assert "private-primary" not in repr(config)
    assert "private-fallback" not in repr(config)
    assert "private-jupiter" not in repr(config)
    config.validate()
    monkeypatch.delenv("NOEMA_SOLANA_RPC_FALLBACK_URL")
    assert TrenchCollectorConfig.from_env().solana_rpc_fallback_url is None
