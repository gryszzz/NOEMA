from noema.doctor import doctor_report
from noema.local_env import load_local_env
from noema.setup_wizard import write_local_env


def test_doctor_never_returns_foundry_secret(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env.local"
    pem = tmp_path / "kalshi.pem"
    pem.write_text("test")
    write_local_env(
        {
            "NOEMA_FOUNDRY_ENDPOINT": "https://example.openai.azure.com",
            "NOEMA_FOUNDRY_DEPLOYMENT": "astra",
            "NOEMA_FOUNDRY_API_KEY": "super-secret",
            "KALSHI_API_KEY_ID": "kid",
            "KALSHI_PRIVATE_KEY_PATH": str(pem),
            "NOEMA_EVM_RPC_URL": "https://rpc.example",
            "NOEMA_EVM_ADDRESS": "0x1111111111111111111111111111111111111111",
        },
        path=str(env_path),
    )
    for key in (
        "NOEMA_FOUNDRY_ENDPOINT",
        "NOEMA_FOUNDRY_DEPLOYMENT",
        "NOEMA_FOUNDRY_API_KEY",
        "KALSHI_API_KEY_ID",
        "KALSHI_PRIVATE_KEY_PATH",
        "NOEMA_EVM_RPC_URL",
        "NOEMA_EVM_ADDRESS",
    ):
        monkeypatch.delenv(key, raising=False)
    load_local_env(str(env_path))
    report = doctor_report(str(tmp_path / "noema.db"))
    assert "super-secret" not in str(report)
    assert any(check["name"] == "foundry" for check in report["checks"])
    assert any(
        check["name"] == "model_budget" and check["status"] == "missing"
        for check in report["checks"]
    )
