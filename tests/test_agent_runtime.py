from noema.agent_config import AgentConfig


def test_agent_config_requires_evm_pair() -> None:
    config = AgentConfig(evm_rpc_url="https://rpc.example")
    try:
        config.validate()
    except ValueError as exc:
        assert "configured together" in str(exc)
    else:
        raise AssertionError("expected config validation failure")
