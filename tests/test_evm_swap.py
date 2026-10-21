"""EVM aggregator swap signer tests. No test in this module contacts mainnet."""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from noema import wallet_signer_process
from noema.evm_swap import (
    EXPECTED_EVM_SIGNER_ADDRESS,
    KYBER_NATIVE,
    KYBER_ROUTER,
    SWAP_CHAINS,
    perform_evm_swap,
)
from noema.wallet_signer import LocalEvmWalletSigner, LocalSolanaWalletSigner

TOKEN = to_checksum_address("0x1111111111111111111111111111111111111111")
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@pytest.fixture(autouse=True)
def _block_mainnet(monkeypatch):
    real_client = httpx.Client

    def guarded(*args, **kwargs):
        client = real_client(*args, **kwargs)
        original = client.request

        def request(method, url, *a, **kw):
            host = str(url)
            blocked = (
                "aggregator-api.kyberswap.com", "api.kyberswap.com", "api.0x.org",
                "api.1inch.dev", "mainnet.base.org", "publicnode.com", "infura.io",
                "alchemy.com", "rpc.mainnet.chain.robinhood.com", "llamarpc.com",
            )
            if any(item in host for item in blocked):
                raise AssertionError("test attempted a live network call")
            return original(method, url, *a, **kw)

        client.request = request
        return client

    monkeypatch.setattr(httpx, "Client", guarded)


def _word(address: str) -> str:
    return "000000000000000000000000" + address[2:].lower()


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def close(self):
        return None

    def get(self, url, params=None, headers=None):
        self.calls.append(("GET", str(url)))
        return _Response(self.handler("GET", str(url), params, None, headers))

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", str(url)))
        return _Response(self.handler("POST", str(url), None, json, headers))


def _arm(monkeypatch, tmp_path, account, *, trade="100", daily="100"):
    monkeypatch.setattr(
        "noema.evm_swap.EXPECTED_EVM_SIGNER_ADDRESS", account.address,
    )
    monkeypatch.setenv("NOEMA_EVM_ADDRESS", account.address)
    monkeypatch.setenv("NOEMA_EVM_SIGNER_ENABLED", "1")
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_EVM_OWNER_REVIEWED", "1")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "base")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_EVM_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_WALLET_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_EVM_MAX_TRADE_USD", trade)
    monkeypatch.setenv("NOEMA_EVM_MAX_DAILY_USD", daily)
    monkeypatch.setenv(
        "NOEMA_EVM_NATIVE_PRICES_USD",
        '{"ethereum":"3000","base":"3000","polygon":"0.40","robinhood":"3000"}',
    )
    monkeypatch.setenv("NOEMA_EVM_TOKEN_ALLOWLIST", f"base:{TOKEN}")
    monkeypatch.setenv("NOEMA_EVM_RPC_URL_BASE", "http://127.0.0.1/rpc")
    monkeypatch.setenv("NOEMA_KYBERSWAP_API_URL", "http://127.0.0.1/kyber")
    monkeypatch.setenv("NOEMA_EVM_SIGNER_STATE_PATH", str(tmp_path / "evm-signer.db"))
    monkeypatch.setenv("NOEMA_EVM_CONFIRM_ATTEMPTS", "1")
    monkeypatch.setenv("NOEMA_EVM_CONFIRM_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("NOEMA_KYBERSWAP_API_KEY", "kyber-key-should-not-leak")


def _service(account, *, confirm="confirmed", router=KYBER_ROUTER):
    state = {"quotes": 0, "builds": 0, "sends": 0, "raw": None}

    def handler(method, url, params, body, headers):
        if headers and "kyber-key-should-not-leak" not in json.dumps(headers):
            pass
        blob = json.dumps({"params": params, "body": body, "headers": headers}, default=str)
        assert "SIGNER_SECRET" not in blob
        if method == "GET" and url.endswith("/routes"):
            state["quotes"] += 1
            return {
                "code": 0,
                "data": {
                    "routerAddress": router,
                    "routeSummary": {
                        "tokenIn": params["tokenIn"],
                        "tokenOut": params["tokenOut"],
                        "amountIn": params["amountIn"],
                        "amountOut": "1000000",
                    },
                },
            }
        if method == "POST" and str(url).endswith("/route/build"):
            state["builds"] += 1
            summary = body["routeSummary"]
            native = summary["tokenIn"].lower() == KYBER_NATIVE.lower()
            value = summary["amountIn"] if native else "0"
            return {
                "code": 0,
                "data": {
                    "routerAddress": router,
                    "transactionValue": value,
                    "amountOut": summary["amountOut"],
                    "data": "0xe21fd0e9" + _word(body["sender"]),
                },
            }
        rpc = body["method"]
        if rpc == "eth_chainId":
            return {"result": hex(8453)}
        if rpc == "eth_getCode":
            return {"result": "0x60016000"}
        if rpc == "eth_call":
            data = body["params"][0]["data"]
            if data == "0x313ce567":
                return {"result": hex(6)}
            if data.startswith("0x70a08231"):
                return {"result": hex(10**24)}
            if data.startswith("0xdd62ed3e"):
                return {"result": hex(10**24)}
            return {"result": "0x"}
        if rpc == "eth_getBalance":
            return {"result": hex(10**19)}
        if rpc == "eth_estimateGas":
            return {"result": hex(100_000)}
        if rpc == "eth_gasPrice":
            return {"result": hex(10**9)}
        if rpc == "eth_getTransactionCount":
            return {"result": "0x3"}
        if rpc == "eth_sendRawTransaction":
            state["sends"] += 1
            raw = bytes.fromhex(body["params"][0][2:])
            state["raw"] = body["params"][0]
            return {"result": "0x" + keccak(raw).hex()}
        if rpc == "eth_getTransactionReceipt":
            if confirm != "confirmed":
                return {"result": None}
            wallet = "0x" + _word(account.address)
            return {"result": {
                "status": "0x1",
                "gasUsed": hex(50_000),
                "effectiveGasPrice": hex(10**9),
                "blockNumber": "0x11",
                "logs": [{
                    "address": TOKEN,
                    "topics": [TRANSFER, "0x" + _word(KYBER_ROUTER), wallet],
                    "data": hex(1_000_000),
                }],
            }}
        raise AssertionError(rpc)

    return _Client(handler), state


def test_expected_address_is_the_owner_wallet():
    assert EXPECTED_EVM_SIGNER_ADDRESS == "0x92B9a0C66a37fE9635e55045b3bf40cB035a59C8"
    assert SWAP_CHAINS["robinhood"].chain_id == 4663
    assert SWAP_CHAINS["base"].chain_id == 8453
    assert SWAP_CHAINS["ethereum"].chain_id == 1
    assert SWAP_CHAINS["polygon"].chain_id == 137


def test_secret_never_leaves_the_signer_process(monkeypatch, capsys):
    monkeypatch.setenv("NOEMA_EVM_SIGNER_SECRET", "0x" + "ab" * 31 + "zz")
    payload = json.dumps({"operation": "evm_aggregator_swap", "side": "buy"}).encode()
    monkeypatch.setattr(sys, "stdin", __import__("io").TextIOWrapper(__import__("io").BytesIO(payload)))
    assert wallet_signer_process.main() == 1
    captured = capsys.readouterr()
    assert "ab" * 10 not in captured.out
    assert "valid private key" in captured.out


def test_refuses_a_key_that_is_not_the_expected_address(monkeypatch, tmp_path):
    account = Account.create()
    _arm(monkeypatch, tmp_path, account)
    monkeypatch.setattr(
        "noema.evm_swap.EXPECTED_EVM_SIGNER_ADDRESS",
        "0x92B9a0C66a37fE9635e55045b3bf40cB035a59C8",
    )
    monkeypatch.setenv("NOEMA_EVM_ADDRESS", account.address)
    with pytest.raises(RuntimeError, match="does not match"):
        perform_evm_swap({
            "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
            "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "addr-1",
        }, account, client=_service(account)[0])


def test_swap_signs_sends_and_reconciles_token_amount(monkeypatch, tmp_path):
    account = Account.create()
    _arm(monkeypatch, tmp_path, account)
    client, state = _service(account)
    result = perform_evm_swap({
        "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
        "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "desk:base:1",
        "rpc_url": "https://mainnet.base.org", "secret": "do-not-forward",
    }, account, client=client)
    assert result["status"] == "confirmed"
    assert result["token_delta_raw"] == "1000000"
    assert result["token_decimals"] == 6
    assert result["chain_id"] == 8453
    assert int(result["native_delta_wei"]) < 0
    assert state["sends"] == 1 and state["builds"] == 1 and state["quotes"] == 1
    assert "do-not-forward" not in json.dumps(result)
    assert "kyber-key-should-not-leak" not in json.dumps(result)
    assert "raw_tx" not in result
    assert all(url.startswith("http://127.0.0.1/") for _, url in client.calls)
    again = perform_evm_swap({
        "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
        "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "desk:base:1",
    }, account, client=client)
    assert again["transaction_hash"] == result["transaction_hash"]
    assert state["sends"] == 1 and state["builds"] == 1


def test_retry_resends_the_same_transaction_without_a_second_build(monkeypatch, tmp_path):
    account = Account.create()
    _arm(monkeypatch, tmp_path, account)
    client, state = _service(account, confirm="timeout")
    request = {
        "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
        "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "retry-1",
    }
    with pytest.raises(RuntimeError, match="confirmation timed out"):
        perform_evm_swap(request, account, client=client)
    assert state["sends"] == 1 and state["builds"] == 1
    first = state["raw"]

    def handler(method, url, params, body, headers):
        if method == "GET" or (method == "POST" and not (body or {}).get("jsonrpc")):
            raise AssertionError("retry rebuilt a swap")
        rpc = body["method"]
        if rpc == "eth_sendRawTransaction":
            state["sends"] += 1
            assert body["params"][0] == first
            raw = bytes.fromhex(body["params"][0][2:])
            return {"result": "0x" + keccak(raw).hex()}
        if rpc == "eth_getTransactionReceipt":
            wallet = "0x" + _word(account.address)
            return {"result": {
                "status": "0x1", "gasUsed": hex(50_000), "effectiveGasPrice": hex(10**9),
                "blockNumber": "0x12",
                "logs": [{
                    "address": TOKEN,
                    "topics": [TRANSFER, "0x" + _word(KYBER_ROUTER), wallet],
                    "data": hex(1_000_000),
                }],
            }}
        raise AssertionError(rpc)

    client.handler = handler
    result = perform_evm_swap(request, account, client=client)
    assert result["status"] == "confirmed"
    assert state["builds"] == 1 and state["sends"] == 2


def test_signer_guards_are_independent_of_the_request(monkeypatch, tmp_path):
    account = Account.create()
    _arm(monkeypatch, tmp_path, account, trade="1", daily="1")
    client, state = _service(account)
    with pytest.raises(RuntimeError, match="per-trade cap exceeded"):
        perform_evm_swap({
            "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**18,
            "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "cap-1",
            "max_trade_usd": "999999",
        }, account, client=client)
    assert state["quotes"] == 0 and state["sends"] == 0

    monkeypatch.setenv("NOEMA_EVM_MAX_TRADE_USD", "100")
    monkeypatch.setenv("NOEMA_EVM_MAX_DAILY_USD", "100")
    monkeypatch.setenv("NOEMA_EVM_OWNER_REVIEWED", "0")
    with pytest.raises(RuntimeError, match="owner review"):
        perform_evm_swap({
            "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
            "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "review-1",
            "owner_reviewed": True,
        }, account, client=client)
    monkeypatch.setenv("NOEMA_EVM_OWNER_REVIEWED", "1")
    monkeypatch.setenv("NOEMA_EVM_MASTER_HALT", "1")
    with pytest.raises(RuntimeError, match="master halt"):
        perform_evm_swap({
            "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
            "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "halt-1",
        }, account, client=client)
    monkeypatch.setenv("NOEMA_EVM_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_EVM_TOKEN_DENYLIST", f"base:{TOKEN}")
    with pytest.raises(RuntimeError, match="denylisted"):
        perform_evm_swap({
            "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
            "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "deny-1",
        }, account, client=client)


def test_wrong_router_is_not_signed(monkeypatch, tmp_path):
    account = Account.create()
    _arm(monkeypatch, tmp_path, account)
    client, state = _service(account, router=to_checksum_address("0x2222222222222222222222222222222222222222"))
    with pytest.raises(RuntimeError, match="router"):
        perform_evm_swap({
            "chain": "base", "side": "buy", "token": TOKEN, "amount": 10**15,
            "max_slippage_bps": 50, "min_out": 1, "idempotency_key": "router-1",
        }, account, client=client)
    assert state["sends"] == 0 and state["builds"] == 0


def test_parent_ipc_allowlist_excludes_the_secret(monkeypatch):
    secret = "0x" + "ab" * 32
    monkeypatch.setenv("NOEMA_EVM_SIGNER_SECRET", secret)
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", "sol-secret-LEAKME")
    monkeypatch.setenv("NOEMA_KYBERSWAP_API_KEY", "kyber-secret")
    monkeypatch.setenv("NOEMA_EVM_RPC_URL_BASE", "http://127.0.0.1/rpc")
    seen = {}

    def fake_run(cmd, input, text, capture_output, env, timeout, check):
        seen["input"] = input
        seen["env"] = env

        class Result:
            returncode = 0
            stdout = '{"status":"confirmed","transaction_hash":"0xabc"}'

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    signer = LocalEvmWalletSigner()
    result = signer.evm_swap_sync(
        chain="base", side="buy", token=TOKEN, amount=1, max_slippage_bps=10,
        min_out=1, idempotency_key="ipc-1",
    )
    payload = json.loads(seen["input"])
    assert set(payload) == {
        "operation", "chain", "side", "token", "amount", "max_slippage_bps",
        "min_out", "idempotency_key",
    }
    assert secret not in seen["input"]
    assert seen["env"]["NOEMA_EVM_SIGNER_SECRET"] == secret
    assert seen["env"]["NOEMA_KYBERSWAP_API_KEY"] == "kyber-secret"
    assert "NOEMA_SOLANA_SIGNER_SECRET" not in seen["env"]
    assert secret not in json.dumps(result)
    LocalSolanaWalletSigner._run_sync(signer, {"operation": "evm_balances"})
    assert "NOEMA_EVM_SIGNER_SECRET" not in seen["env"]
