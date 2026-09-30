from noema.agent_config import AgentConfig
from noema.agent_runtime import bootstrap_hosted_bill_budget
from noema.bill_tracker import BillTracker


def test_agent_config_requires_evm_pair() -> None:
    config = AgentConfig(evm_rpc_url="https://rpc.example")
    try:
        config.validate()
    except ValueError as exc:
        assert "configured together" in str(exc)
    else:
        raise AssertionError("expected config validation failure")


def test_render_budget_bootstrap_runs_only_for_hosted_runtime_and_persists_once(
    tmp_path, monkeypatch,
):
    db = str(tmp_path / "render.db")
    monkeypatch.delenv("RENDER", raising=False)
    assert bootstrap_hosted_bill_budget(db) is None
    assert not (tmp_path / "render.db").exists()

    monkeypatch.setenv("RENDER", "true")
    values = {
        "NOEMA_HOSTED_BILL_BUDGET_HOSTING_USD": "18.00",
        "NOEMA_HOSTED_BILL_BUDGET_OTHER_USD": "6.00",
        "NOEMA_HOSTED_BILL_BUDGET_MODEL_USD": "0.50",
        "NOEMA_HOSTED_BILL_BUDGET_OWNER_LIMIT_USD": "28.00",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert bootstrap_hosted_bill_budget(db) == "initialized"

    tracker = BillTracker(db)
    assert tracker.overview()["hosting_estimate_usd"] == "18.00"
    tracker.conn.close()
    monkeypatch.setenv("NOEMA_HOSTED_BILL_BUDGET_HOSTING_USD", "99.00")
    assert bootstrap_hosted_bill_budget(db) == "persisted_budget_retained"
    tracker = BillTracker(db)
    assert tracker.overview()["hosting_estimate_usd"] == "18.00"
    tracker.conn.close()
