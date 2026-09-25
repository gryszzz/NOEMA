from noema.config import KalshiConfig
from noema.diagnostics import diagnose


def test_demo_config_never_reports_production_ready() -> None:
    result = diagnose(KalshiConfig(environment="demo"))
    assert result.production_execution_possible is False


def test_master_halt_blocks_production_readiness(tmp_path) -> None:
    key = tmp_path / "key.pem"
    key.write_text("not a real key")
    result = diagnose(
        KalshiConfig(
            environment="production",
            key_id="abc",
            private_key_path=str(key),
            allow_live_orders=True,
            master_halt=True,
        )
    )
    assert result.production_execution_possible is False
