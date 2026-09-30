from eth_account import Account

from noema import wallet_signer_process


def test_evm_transaction_preflight_binds_to_base_chain_and_estimates_gas(monkeypatch) -> None:
    account = Account.create()
    seen: list[tuple[str, list]] = []

    def rpc(_client, _endpoint, method, params):
        seen.append((method, params))
        return {
            "eth_chainId": "0x2105",
            "eth_call": "0x",
            "eth_estimateGas": "0x5208",
            "eth_gasPrice": "0x5b8d80",
            "eth_getBalance": hex(10**18),
        }[method]

    monkeypatch.setattr(wallet_signer_process, "_evm_rpc", rpc)
    import httpx

    with httpx.Client() as client:
        tx, chain_id, gas_limit, gas_price = wallet_signer_process._evm_transaction_parts(
            client,
            {
                "chain": "base",
                "rpc_url": "https://rpc.example",
                "expected_address": account.address,
                "source": account.address,
                "destination": account.address,
                "action": "evm_native_transfer",
                "amount_wei": 0,
                "operational_validation": True,
            },
            account,
        )
    assert chain_id == 8453
    assert gas_limit == 21_000
    assert gas_price == 6_000_000
    assert tx["to"] == account.address
    assert [name for name, _ in seen] == [
        "eth_chainId", "eth_call", "eth_estimateGas", "eth_gasPrice", "eth_getBalance",
    ]


def test_evm_transaction_preflight_rejects_wrong_chain_id(monkeypatch) -> None:
    account = Account.create()
    monkeypatch.setattr(wallet_signer_process, "_evm_rpc", lambda *_: "0x1")
    import httpx
    import pytest

    with httpx.Client() as client, pytest.raises(RuntimeError, match="chain ID"):
        wallet_signer_process._evm_transaction_parts(
            client,
            {
                "chain": "base",
                "rpc_url": "https://rpc.example",
                "expected_address": account.address,
                "source": account.address,
                "destination": account.address,
                "action": "evm_native_transfer",
                "amount_wei": 0,
            },
            account,
        )


def test_zero_value_evm_transfer_requires_diagnostic_self_call(monkeypatch) -> None:
    import httpx
    import pytest

    account = Account.create()
    methods = iter(["0x2105"])
    monkeypatch.setattr(wallet_signer_process, "_evm_rpc", lambda *_: next(methods))
    with httpx.Client() as client, pytest.raises(RuntimeError, match="zero-value EVM"):
        wallet_signer_process._evm_transaction_parts(
            client,
            {
                "chain": "base",
                "rpc_url": "https://rpc.example",
                "expected_address": account.address,
                "source": account.address,
                "destination": account.address,
                "action": "evm_native_transfer",
                "amount_wei": 0,
            },
            account,
        )


def test_evm_rpc_transport_error_is_sanitized() -> None:
    import httpx
    import pytest

    class BrokenClient:
        def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "https://rpc.example")
            raise httpx.ConnectError("private endpoint detail", request=request)

    with pytest.raises(RuntimeError, match="EVM RPC request failed"):
        wallet_signer_process._evm_rpc(BrokenClient(), "https://rpc.example", "eth_chainId", [])


def test_all_write_operations_remain_disabled_until_authority_gate_is_wired() -> None:
    import pytest

    for operation in (
        wallet_signer_process.execute_native_transfer,
        wallet_signer_process.execute_evm_transaction,
        wallet_signer_process.execute_bitcoin_transaction,
    ):
        with pytest.raises(RuntimeError, match="canonical wallet authority gate is not wired"):
            operation({})
