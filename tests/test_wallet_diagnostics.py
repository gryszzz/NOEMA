from noema.wallet_diagnostics import project_wallet_capabilities, public_wallet_policy


def test_public_policy_contains_no_secret_material() -> None:
    view = public_wallet_policy()
    assert view["master_halt"] is True
    assert view["signer_state"] in {"disabled", "configured_execution_disabled", "unconfigured"}
    assert view["signing_enabled"] is False
    assert view["live_execution_enabled"] is False
    assert view["coordinator_wired"] is False
    assert view["mission_authority_present"] is False
    assert view["delegated_capital_authority"] is False
    assert "private_key" not in view
    assert "secret" not in view


def test_signer_configured_is_not_live_execution_enabled(monkeypatch) -> None:
    from noema import wallet_diagnostics

    monkeypatch.setattr(wallet_diagnostics, "solana_signing_credential_present", lambda: False)
    monkeypatch.setattr(wallet_diagnostics, "evm_signing_credential_present", lambda: True)
    monkeypatch.setattr(wallet_diagnostics, "bitcoin_signing_credential_present", lambda: False)
    view = public_wallet_policy()
    base = next(row for row in view["wallet_networks"] if row["chain"] == "base")
    assert base["signer_configured"] is True
    assert base["live_execution_enabled"] is False
    assert view["coordinator_wired"] is False
    assert view["mission_authority_present"] is False


def test_funded_wallet_projection_does_not_imply_authority() -> None:
    view = project_wallet_capabilities(
        connected=True, authenticated=False, readable=True, funded=True,
        signer_configured=True, credentials_isolated=True, halted=False, research_enabled=True,
    )
    assert view["funded"] is True
    assert view["mission_authority_present"] is False
    assert view["live_execution_enabled"] is False
    assert view["coordinator_wired"] is False
