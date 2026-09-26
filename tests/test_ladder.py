from noema.ladder import build_ladder_report


def test_fresh_install_has_actionable_readiness_without_claiming_live(tmp_path, monkeypatch) -> None:
    for name in (
        "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "NOEMA_EVM_RPC_URL",
        "NOEMA_EVM_ADDRESS", "NOEMA_FOUNDRY_ENDPOINT", "NOEMA_FOUNDRY_DEPLOYMENT",
        "NOEMA_FOUNDRY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    report = build_ladder_report(str(tmp_path / "noema.db"))
    steps = {step["rung"]: step for step in report["steps"]}
    assert steps["observe"]["status"] == "pending"
    assert steps["observe"]["next_action"] == "noema agent-once"
    assert steps["live_capital"]["status"] == "locked"
