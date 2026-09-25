from noema.config import KalshiConfig
from noema.diagnostics import diagnose


def test_demo_config_without_credentials_is_not_ready() -> None:
    result = diagnose(KalshiConfig(environment="demo"))
    assert result.demo_ready is False


def test_demo_credentials_report_ready(tmp_path) -> None:
    key = tmp_path / "key.pem"
    key.write_text("placeholder")
    result = diagnose(
        KalshiConfig(
            environment="demo",
            key_id="abc",
            private_key_path=str(key),
            allow_live_orders=False,
            master_halt=True,
        )
    )
    assert result.demo_ready is True
    assert result.master_halt is True
