from noema.wallet_diagnostics import public_wallet_policy


def test_public_policy_contains_no_secret_material() -> None:
    view = public_wallet_policy()
    assert view["master_halt"] is True
    assert view["signer_state"] == "disabled"
    assert "private_key" not in view
    assert "secret" not in view
