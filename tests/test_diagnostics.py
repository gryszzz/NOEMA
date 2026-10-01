from noema.config import KalshiConfig
from noema.diagnostics import diagnose, kalshi_runtime_credential_diagnostic


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


def test_runtime_credential_diagnostic_contains_only_safe_presence_metadata(
    monkeypatch, tmp_path,
) -> None:
    from noema import wallet_credentials

    pem_path = tmp_path / "kalshi.pem"
    pem_path.write_text("private-key-material-must-not-appear")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("NOEMA_KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "fixture-id-must-not-appear")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr(wallet_credentials, "RENDER_KALSHI_SECRET_FILE", pem_path)

    result = kalshi_runtime_credential_diagnostic(KalshiConfig.from_env())

    assert result == {
        "kalshi_api_key_id_present": "yes",
        "kalshi_api_key_id_header_safe": "yes",
        "kalshi_api_key_id_provider": "environment",
        "kalshi_private_key_configured": "yes",
        "kalshi_private_key_provider": "render_secret_file",
        "kalshi_private_key_file_status": "readable",
        "kalshi_environment": "production",
    }
    assert "fixture-id-must-not-appear" not in repr(result)
    assert "private-key-material-must-not-appear" not in repr(result)


def test_runtime_credential_diagnostic_reports_header_safety_without_key_id(monkeypatch):
    result = kalshi_runtime_credential_diagnostic(KalshiConfig(
        environment="production",
        key_id="fixture-id\nsecret-suffix",
    ))
    assert result["kalshi_api_key_id_header_safe"] == "no"
    assert "fixture-id" not in repr(result)
    assert "secret-suffix" not in repr(result)
