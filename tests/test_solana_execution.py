"""Jupiter swap signer tests. No test in this module contacts mainnet."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from noema import wallet_signer_process
from noema.economic_ledger import EconomicLedger
from noema.execution_gateway import ExecutionGateway
from noema.solana_execution import JupiterDeskExecutor
from noema.solana_live_gate import solana_desk_live_enabled
from noema.solana_research import PoolCandidate
from noema.trench_desk_runtime import DeskRuntimeConfig, run_desk_cycle
from noema.trench_desks import Mark, TrenchDeskStore
from noema.trench_models import TokenControlState
from noema.wallet_intents import WalletIntent
from noema.wallet_signer import LocalSolanaWalletSigner
from noema.wallet_types import Chain

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    chars: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        chars.append(_B58[remainder])
    pad = 0
    for byte in data:
        if byte:
            break
        pad += 1
    return "1" * pad + "".join(reversed(chars))


@pytest.fixture(autouse=True)
def _block_mainnet(monkeypatch):
    real_client = httpx.Client

    def guarded(*args, **kwargs):
        client = real_client(*args, **kwargs)
        original = client.request

        def request(method, url, *a, **kw):
            host = str(url)
            if any(blocked in host for blocked in (
                "mainnet-beta.solana.com", "quote-api.jup.ag", "api.jup.ag",
            )):
                raise AssertionError("test attempted a live network call")
            return original(method, url, *a, **kw)

        client.request = request
        return client

    monkeypatch.setattr(httpx, "Client", guarded)


def _unsigned(payer: Keypair, mint: Pubkey) -> str:
    instruction = Instruction(
        program_id=Pubkey.from_string(TOKEN_PROGRAM),
        data=bytes([1]),
        accounts=[
            AccountMeta(payer.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
        ],
    )
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [], Hash.default())
    transaction = VersionedTransaction.populate(message, [Signature.default()])
    return base64.b64encode(bytes(transaction)).decode()


def _mint_account(mint: str, *, freeze=None, mint_authority=None, extensions=None, owner=TOKEN_PROGRAM):
    info = {
        "decimals": 6, "freezeAuthority": freeze, "isInitialized": True,
        "mintAuthority": mint_authority, "supply": "1000",
    }
    if extensions is not None:
        info["extensions"] = extensions
    return {
        "owner": owner,
        "data": {"parsed": {"type": "mint", "info": info}, "program": "spl-token"},
    }


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

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self):
        return None

    def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params, headers))
        return _Response(self.handler("GET", url, params, None, headers))

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json, headers))
        return _Response(self.handler("POST", url, None, json, headers))


def _arm(monkeypatch, tmp_path, signer: Keypair, *, trade="100", daily="100", price="100"):
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", json.dumps(list(bytes(signer))))
    monkeypatch.setenv("NOEMA_SOLANA_WALLET_ADDRESS", str(signer.pubkey()))
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_ENABLED", "1")
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "solana")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_SOLANA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_WALLET_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_SOLANA_MAX_TRADE_USD", trade)
    monkeypatch.setenv("NOEMA_SOLANA_MAX_DAILY_USD", daily)
    monkeypatch.setenv("NOEMA_SOLANA_PRICE_USD", price)
    monkeypatch.setenv("NOEMA_SOLANA_RPC_URL", "http://127.0.0.1/rpc")
    monkeypatch.setenv("NOEMA_JUPITER_SWAP_API_URL", "http://127.0.0.1/jupiter")
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_STATE_PATH", str(tmp_path / "signer.db"))
    monkeypatch.setenv("NOEMA_SOLANA_CONFIRM_ATTEMPTS", "1")
    monkeypatch.setenv("NOEMA_SOLANA_CONFIRM_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("NOEMA_SOLANA_FEE_RESERVE_LAMPORTS", "20000000")
    monkeypatch.setenv("NOEMA_JUPITER_API_KEY", "jupiter-key-should-not-leak")
    monkeypatch.delenv("NOEMA_AUTONOMY_AUTHORITY_PATH", raising=False)
    monkeypatch.delenv("NOEMA_AUTONOMY_LEVEL", raising=False)
    monkeypatch.delenv("NOEMA_SOLANA_BANKROLL_USD", raising=False)


def _service(signer: Keypair, mint: str, *, confirm="confirmed", swap_fee_payer: Keypair | None = None):
    unsigned = _unsigned(swap_fee_payer or signer, Pubkey.from_string(mint))
    state = {"sends": 0, "swaps": 0, "quotes": 0}

    def handler(method, url, params, body, headers):
        if str(url).endswith("/quote"):
            state["quotes"] += 1
            assert headers.get("x-api-key") == "jupiter-key-should-not-leak"
            return {
                "inputMint": params["inputMint"], "outputMint": params["outputMint"],
                "inAmount": params["amount"], "outAmount": "1000000",
                "otherAmountThreshold": "900000",
            }
        if str(url).endswith("/swap"):
            state["swaps"] += 1
            assert body["prioritizationFeeLamports"] == 100_000
            assert body["userPublicKey"] == str(signer.pubkey())
            assert "x-api-key" in (headers or {})
            return {"swapTransaction": unsigned}
        assert str(url).startswith("http://127.0.0.1/rpc")
        assert "x-api-key" not in (headers or {})
        rpc = body["method"]
        if rpc == "getAccountInfo":
            return {"result": {"value": _mint_account(mint)}}
        if rpc == "getBalance":
            return {"result": {"value": 2_000_000_000}}
        if rpc == "simulateTransaction":
            return {"result": {"value": {"err": None, "unitsConsumed": 10}}}
        if rpc == "sendTransaction":
            state["sends"] += 1
            raw = base64.b64decode(body["params"][0])
            signed = VersionedTransaction.from_bytes(raw)
            assert signed.verify_with_results() == [True]
            return {"result": str(signed.signatures[0])}
        if rpc == "getSignatureStatuses":
            if confirm == "timeout":
                return {"result": {"value": [None]}}
            if confirm == "err":
                return {"result": {"value": [{"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": "confirmed"}]}}
            return {"result": {"value": [{"err": None, "confirmationStatus": confirm}]}}
        if rpc == "getTransaction":
            signature = body["params"][0]
            address = str(signer.pubkey())
            return {"result": {
                "slot": 42,
                "transaction": {"message": {"accountKeys": [address]}, "signatures": [signature]},
                "meta": {
                    "err": None, "fee": 5000,
                    "preBalances": [2_000_000_000], "postBalances": [1_940_000_000],
                    "preTokenBalances": [],
                    "postTokenBalances": [{
                        "mint": mint, "owner": address,
                        "uiTokenAmount": {"amount": "1000000", "decimals": 6},
                    }],
                },
            }}
        raise AssertionError(rpc)

    return _Client(handler), state


def test_json_and_base58_secrets_match_configured_wallet(monkeypatch):
    signer = Keypair()
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", json.dumps(list(bytes(signer))))
    assert str(wallet_signer_process.load_solana_keypair_from_env().pubkey()) == str(signer.pubkey())
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", _b58encode(bytes(signer)))
    assert str(wallet_signer_process.load_solana_keypair_from_env().pubkey()) == str(signer.pubkey())
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", "not-a-real-key-LEAKME")
    with pytest.raises(RuntimeError, match="not a valid keypair"):
        wallet_signer_process.load_solana_keypair_from_env()


def test_secret_never_leaves_the_signer_process(monkeypatch, capsys):
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", "visible-secret-LEAKME")
    payload = json.dumps({"operation": "solana_jupiter_swap", "side": "buy"}).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
    assert wallet_signer_process.main() == 1
    captured = capsys.readouterr()
    assert "LEAKME" not in captured.out
    assert "LEAKME" not in captured.err
    assert "valid keypair" in captured.out


def test_swap_signs_sends_and_reconciles_token_amount(monkeypatch, tmp_path):
    signer = Keypair()
    mint = str(Keypair().pubkey())
    _arm(monkeypatch, tmp_path, signer)
    client, state = _service(signer, mint)
    from noema.solana_swap import perform_jupiter_swap

    result = perform_jupiter_swap({
        "side": "buy", "mint": mint, "amount": 60_000_000, "max_slippage_bps": 100,
        "min_out": 1, "idempotency_key": "desk:vet:0:buy",
        "rpc_url": "https://api.mainnet-beta.solana.com", "secret": "do-not-forward",
        "max_trade_usd": "999999",
    }, signer, client=client)
    assert result["status"] == "confirmed"
    assert result["token_delta_raw"] == "1000000"
    assert result["token_decimals"] == 6
    assert result["sol_delta_lamports"] == -60_000_000
    assert result["notional_usd"] == "6"
    assert state["sends"] == 1 and state["swaps"] == 1
    assert "do-not-forward" not in json.dumps(result)
    assert "jupiter-key-should-not-leak" not in json.dumps(result)
    assert "signed_tx" not in result
    assert all(call[1].startswith("http://127.0.0.1/") for call in client.calls)
    again = perform_jupiter_swap({
        "side": "buy", "mint": mint, "amount": 60_000_000, "max_slippage_bps": 100,
        "min_out": 1, "idempotency_key": "desk:vet:0:buy",
    }, signer, client=client)
    assert again["signature"] == result["signature"]
    assert state["sends"] == 1 and state["swaps"] == 1 and state["quotes"] == 1


def test_retry_resends_the_same_transaction_without_a_second_swap(monkeypatch, tmp_path):
    signer = Keypair()
    mint = str(Keypair().pubkey())
    _arm(monkeypatch, tmp_path, signer)
    client, state = _service(signer, mint, confirm="timeout")
    from noema.solana_swap import perform_jupiter_swap

    request = {
        "side": "buy", "mint": mint, "amount": 60_000_000, "max_slippage_bps": 50,
        "min_out": 1, "idempotency_key": "desk:retry:1",
    }
    with pytest.raises(RuntimeError, match="confirmation timed out"):
        perform_jupiter_swap(request, signer, client=client)
    assert state["sends"] == 1 and state["swaps"] == 1
    client.handler = _service(signer, mint, confirm="confirmed")[0].handler
    # The replacement handler closes over a new unsigned tx; keep the stored one by
    # confirming whatever signature is already signed. Rebuild handler against same tx.
    result = perform_jupiter_swap(request, signer, client=_confirm_existing(client, state, signer, mint))
    assert result["status"] == "confirmed"
    assert state["swaps"] == 1
    assert state["sends"] == 2


def _confirm_existing(client, state, signer, mint):
    def handler(method, url, params, body, headers):
        if str(url).endswith("/quote") or str(url).endswith("/swap"):
            raise AssertionError("retry rebuilt a swap")
        rpc = body["method"]
        if rpc == "sendTransaction":
            state["sends"] += 1
            raw = base64.b64decode(body["params"][0])
            signed = VersionedTransaction.from_bytes(raw)
            return {"result": str(signed.signatures[0])}
        if rpc == "getSignatureStatuses":
            return {"result": {"value": [{"err": None, "confirmationStatus": "confirmed"}]}}
        if rpc == "getTransaction":
            address = str(signer.pubkey())
            return {"result": {
                "slot": 7,
                "transaction": {"message": {"accountKeys": [address]}},
                "meta": {
                    "err": None, "fee": 5000,
                    "preBalances": [2_000_000_000], "postBalances": [1_940_000_000],
                    "preTokenBalances": [],
                    "postTokenBalances": [{
                        "mint": mint, "owner": address,
                        "uiTokenAmount": {"amount": "1000000", "decimals": 6},
                    }],
                },
            }}
        raise AssertionError(rpc)

    client.handler = handler
    return client


def test_signer_guards_are_independent_of_the_request(monkeypatch, tmp_path):
    signer = Keypair()
    mint = str(Keypair().pubkey())
    _arm(monkeypatch, tmp_path, signer, trade="5", daily="5")
    from noema.solana_swap import perform_jupiter_swap

    client, state = _service(signer, mint)
    with pytest.raises(RuntimeError, match="per-trade cap exceeded"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 60_000_000, "max_slippage_bps": 100,
            "min_out": 1, "idempotency_key": "cap-1", "max_trade_usd": "999999",
        }, signer, client=client)
    assert state["quotes"] == 0

    monkeypatch.setenv("NOEMA_SOLANA_WALLET_ADDRESS", str(Keypair().pubkey()))
    with pytest.raises(RuntimeError, match="does not match"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 1_000_000, "max_slippage_bps": 100,
            "min_out": 1, "idempotency_key": "wallet-1",
        }, signer, client=client)

    monkeypatch.setenv("NOEMA_SOLANA_WALLET_ADDRESS", str(signer.pubkey()))
    monkeypatch.setenv("NOEMA_SOLANA_MAX_TRADE_USD", "100")
    monkeypatch.setenv("NOEMA_SOLANA_MAX_DAILY_USD", "100")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "1")
    with pytest.raises(RuntimeError, match="master halt"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 1_000_000, "max_slippage_bps": 100,
            "min_out": 1, "idempotency_key": "halt-1", "master_halt": False,
        }, signer, client=client)
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_SOLANA_MINT_DENYLIST", mint)
    with pytest.raises(RuntimeError, match="denylisted"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 1_000_000, "max_slippage_bps": 100,
            "min_out": 1, "idempotency_key": "deny-1",
        }, signer, client=client)
    monkeypatch.delenv("NOEMA_SOLANA_MINT_DENYLIST")
    with pytest.raises(RuntimeError, match="slippage exceeds"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 1_000_000, "max_slippage_bps": 5_000,
            "min_out": 1, "idempotency_key": "slip-1",
        }, signer, client=client)


def test_freeze_and_mint_authority_and_extension_are_refused(monkeypatch, tmp_path):
    signer = Keypair()
    mint = str(Keypair().pubkey())
    _arm(monkeypatch, tmp_path, signer)
    from noema.solana_swap import perform_jupiter_swap

    def client_for(account):
        def handler(method, url, params, body, headers):
            if body and body.get("method") == "getAccountInfo":
                return {"result": {"value": account}}
            raise AssertionError("guard should fail before a quote")
        return _Client(handler)

    request = {
        "side": "buy", "mint": mint, "amount": 1_000_000, "max_slippage_bps": 50,
        "min_out": 1, "idempotency_key": "mint-guard",
    }
    freeze = str(Keypair().pubkey())
    with pytest.raises(RuntimeError, match="freeze authority"):
        perform_jupiter_swap(request, signer, client=client_for(_mint_account(mint, freeze=freeze)))
    with pytest.raises(RuntimeError, match="mint authority"):
        perform_jupiter_swap(request, signer, client=client_for(_mint_account(mint, mint_authority=freeze)))
    with pytest.raises(RuntimeError, match="token extension"):
        perform_jupiter_swap(request, signer, client=client_for(_mint_account(
            mint, extensions=[{"extension": "transferFeeConfig"}],
        )))


def test_reserve_and_daily_cap_and_authority_fractions(monkeypatch, tmp_path):
    signer = Keypair()
    mint = str(Keypair().pubkey())
    _arm(monkeypatch, tmp_path, signer, trade="100", daily="10", price="100")
    from noema.solana_swap import perform_jupiter_swap

    poor = _service(signer, mint)[0]

    def poor_balance(method, url, params, body, headers):
        if body and body.get("method") == "getBalance":
            return {"result": {"value": 30_000_000}}
        if body and body.get("method") == "getAccountInfo":
            return {"result": {"value": _mint_account(mint)}}
        raise AssertionError(body["method"] if body else url)

    poor.handler = poor_balance
    with pytest.raises(RuntimeError, match="reserve"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 20_000_000, "max_slippage_bps": 50,
            "min_out": 1, "idempotency_key": "reserve-1",
        }, signer, client=poor)

    client, state = _service(signer, mint)
    first = perform_jupiter_swap({
        "side": "buy", "mint": mint, "amount": 60_000_000, "max_slippage_bps": 50,
        "min_out": 1, "idempotency_key": "daily-1",
    }, signer, client=client)
    assert first["status"] == "confirmed"
    with pytest.raises(RuntimeError, match="daily cap exceeded"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 60_000_000, "max_slippage_bps": 50,
            "min_out": 1, "idempotency_key": "daily-2",
        }, signer, client=client)
    assert state["sends"] == 1

    path = tmp_path / "authority.toml"
    path.write_text(
        "[paper]\nlive = false\n\n[micro]\nlive = true\n"
        "per_trade_fraction = \"0.01\"\ndaily_notional_fraction = \"0.10\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOEMA_AUTONOMY_AUTHORITY_PATH", str(path))
    monkeypatch.setenv("NOEMA_AUTONOMY_LEVEL", "paper")
    monkeypatch.setenv("NOEMA_SOLANA_BANKROLL_USD", "1000")
    monkeypatch.delenv("NOEMA_SOLANA_MAX_TRADE_USD")
    monkeypatch.delenv("NOEMA_SOLANA_MAX_DAILY_USD")
    with pytest.raises(RuntimeError, match="does not permit live"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 1_000_000, "max_slippage_bps": 50,
            "min_out": 1, "idempotency_key": "level-paper",
        }, signer, client=client)
    monkeypatch.setenv("NOEMA_AUTONOMY_LEVEL", "micro")
    with pytest.raises(RuntimeError, match="per-trade cap exceeded"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 200_000_000, "max_slippage_bps": 50,
            "min_out": 1, "idempotency_key": "level-micro",
        }, signer, client=client)


def test_wrong_fee_payer_is_not_signed_onto_the_network(monkeypatch, tmp_path):
    signer = Keypair()
    mint = str(Keypair().pubkey())
    _arm(monkeypatch, tmp_path, signer)
    from noema.solana_swap import perform_jupiter_swap

    client, state = _service(signer, mint, swap_fee_payer=Keypair())
    with pytest.raises(RuntimeError, match="fee payer"):
        perform_jupiter_swap({
            "side": "buy", "mint": mint, "amount": 1_000_000, "max_slippage_bps": 50,
            "min_out": 1, "idempotency_key": "payer-1",
        }, signer, client=client)
    assert state["sends"] == 0


def test_parent_ipc_allowlist_excludes_the_secret(monkeypatch):
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", "ipc-secret-LEAKME")
    monkeypatch.setenv("NOEMA_JUPITER_API_KEY", "ipc-jupiter-key")
    seen = {}

    def fake_run(cmd, input, text, capture_output, env, timeout, check):
        seen["input"] = input
        seen["env"] = env

        class Result:
            returncode = 0
            stdout = '{"status":"confirmed","signature":"sig"}'

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    signer = LocalSolanaWalletSigner()
    result = signer.jupiter_swap_sync(
        side="buy", mint="mint", amount=1, max_slippage_bps=10, min_out=1,
        idempotency_key="ipc-1",
    )
    payload = json.loads(seen["input"])
    assert set(payload) == {
        "operation", "side", "mint", "amount", "max_slippage_bps", "min_out", "idempotency_key",
    }
    assert "LEAKME" not in seen["input"]
    assert seen["env"]["NOEMA_SOLANA_SIGNER_SECRET"] == "ipc-secret-LEAKME"
    assert "LEAKME" not in json.dumps(result)
    signer._run_sync({"operation": "balances"})
    assert "NOEMA_SOLANA_SIGNER_SECRET" not in seen["env"]


def test_gateway_dex_swap_blocks_when_unarmed(tmp_path, monkeypatch):
    monkeypatch.delenv("NOEMA_ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("NOEMA_SOLANA_SIGNER_ENABLED", raising=False)
    gateway = ExecutionGateway(str(tmp_path / "gw.db"))
    called = {"n": 0}

    def handler():
        called["n"] += 1
        return {"status": "confirmed"}

    intent = WalletIntent(
        intent_id="desk-1", chain=Chain.SOLANA, venue="solana", action="dex_swap",
        asset_in="SOL", asset_out="mint", notional_usd=Decimal(1),
        expected_slippage_bps=Decimal(50), mission_id="trench-desk:vet",
        evidence_ids=("evidence",), amount_atomic=1, minimum_output_atomic=1,
    )
    result = gateway.submit_dex_swap(intent=intent, handler=handler)
    assert result["status"] == "rejected"
    assert called["n"] == 0


def test_live_desk_records_chain_amounts_and_hides_secret(tmp_path, monkeypatch):
    secret = "desk-secret-LEAKME"
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_SECRET", secret)
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_ENABLED", "1")
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "solana")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_SOLANA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_WALLET_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_SOLANA_PRICE_USD", "150")
    monkeypatch.setenv("NOEMA_SOLANA_MAX_SLIPPAGE_BPS", "100")
    db = str(tmp_path / "desk.db")
    calls = []

    class Signer:
        def jupiter_swap_sync(self, **kwargs):
            calls.append(kwargs)
            assert "secret" not in kwargs
            return {
                "status": "confirmed",
                "signature": "sig-" + kwargs["idempotency_key"],
                "address": "WalletAddress111",
                "mint": kwargs["mint"],
                "side": kwargs["side"],
                "amount_in": str(kwargs["amount"]),
                "amount_out": "1000000",
                "min_out": str(kwargs["min_out"]),
                "fee_lamports": 5000,
                "sol_delta_lamports": -50_000_000,
                "token_delta_raw": "1000000",
                "token_decimals": 6,
                "slot": 9,
                "idempotency_key": kwargs["idempotency_key"],
                "notional_usd": "7.5",
                "sol_price_usd": "150",
                "price_timestamp": datetime.now(UTC).isoformat(),
            }

    def mint_info(_mint):
        return {"decimals": 6, "control": TokenControlState(
            mint_authority_present=False, freeze_authority_present=False,
            permanent_delegate_present=False, transfer_hook_present=False, transfer_fee_bps=0,
        )}

    class _Gecko:
        async def token_top10_fraction(self, mint):
            return None

        async def trending_pools(self):
            created = datetime.now(UTC) - timedelta(hours=5)
            return [PoolCandidate(
                "GOOD", "GOOD", "P1", created, 0.01, 80_000, 1e6, 1.0, 5.0, 10, 90, 9000, "g",
            )]

        async def new_pools(self):
            return []

    class _Solana:
        async def top_account_supply_shares(self, mint):
            return (0.4, 0.05)

    class _Dex:
        async def tokens_by_mint(self, mints):
            return {mint: {"priceUsd": "0.01", "liquidity": {"usd": 80_000}} for mint in mints}

    summary = __import__("asyncio").run(run_desk_cycle(
        db, config=DeskRuntimeConfig(enabled=True, desk_count=1, paper_capital_usd=100,
                                     holder_enrichment_limit=1, judge_top_n=1),
        gecko=_Gecko(), solana=_Solana(), dex=_Dex(),
        executor=JupiterDeskExecutor(db, signer=Signer(), mint_info=mint_info),
    ))
    assert summary["entries"] == 1 and summary["live_execution"] is True
    assert calls and set(calls[0]) == {
        "side", "mint", "amount", "max_slippage_bps", "min_out", "idempotency_key",
    }
    store = TrenchDeskStore(db)
    trades = list(store.conn.execute("SELECT * FROM trench_desk_trades"))
    assert trades and all(row["paper"] == 0 and row["qty"] == 1 and row["signature"] for row in trades)
    assert all(row["amount_out_raw"] == "1000000" for row in trades)
    blob = " ".join(json.dumps(dict(row)) for row in trades)
    events = " ".join(row["payload_json"] for row in store.conn.execute("SELECT payload_json FROM trench_desk_events"))
    store.close()
    ledger = EconomicLedger(db)
    rows = ledger.conn.execute(
        "SELECT * FROM economic_events WHERE event_type='dex_swap_fill'"
    ).fetchall()
    assert len(rows) == len(trades)
    evidence = " ".join(row["evidence_json"] for row in rows)
    assert secret not in blob and secret not in events and secret not in evidence
    assert "1000000" in evidence


def test_partial_flags_and_halt_do_not_paper_fill_or_go_live(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_ENABLED", "1")
    assert solana_desk_live_enabled() is False

    class _Gecko:
        async def token_top10_fraction(self, mint):
            return None

        async def trending_pools(self):
            created = datetime.now(UTC) - timedelta(hours=5)
            return [PoolCandidate(
                "GOOD", "GOOD", "P1", created, 0.01, 80_000, 1e6, 1.0, 5.0, 10, 90, 9000, "g",
            )]

        async def new_pools(self):
            return []

    class _Solana:
        async def top_account_supply_shares(self, mint):
            return (0.4, 0.05, 0.05)

    class _Dex:
        async def tokens_by_mint(self, mints):
            return {mint: {"priceUsd": "0.01", "liquidity": {"usd": 80_000}} for mint in mints}

    db = str(tmp_path / "paper.db")
    summary = __import__("asyncio").run(run_desk_cycle(
        db, config=DeskRuntimeConfig(enabled=True, desk_count=1, paper_capital_usd=100),
        gecko=_Gecko(), solana=_Solana(), dex=_Dex(),
    ))
    assert summary["live_execution"] is False and summary["entries"] == 1
    paper = TrenchDeskStore(db)
    assert paper.conn.execute("SELECT COUNT(*) FROM trench_desk_trades WHERE paper=1").fetchone()[0] == 2
    paper.close()

    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "solana")
    halted = str(tmp_path / "halted.db")
    halted_summary = __import__("asyncio").run(run_desk_cycle(
        halted, config=DeskRuntimeConfig(enabled=True, desk_count=1, paper_capital_usd=100),
        gecko=_Gecko(), solana=_Solana(), dex=_Dex(),
    ))
    assert halted_summary["entries"] == 0 and halted_summary["live_execution"] is False
    halted_store = TrenchDeskStore(halted)
    assert halted_store.conn.execute("SELECT COUNT(*) FROM trench_desk_trades").fetchone()[0] == 0
    halted_store.close()


def test_quarantine_blocks_the_signer(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_SOLANA_PRICE_USD", "150")
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_ENABLED", "1")
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "solana_jupiter")
    monkeypatch.setenv("NOEMA_SOLANA_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_WALLET_MASTER_HALT", "0")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")

    class Signer:
        def jupiter_swap_sync(self, **_kwargs):
            raise AssertionError("quarantined mint reached the signer")

    executor = JupiterDeskExecutor(
        str(tmp_path / "q.db"), signer=Signer(),
        mint_info=lambda _mint: {"decimals": 6, "control": TokenControlState(suspicious_flag=True)},
    )
    with pytest.raises(Exception, match="quarantine"):
        executor.buy(
            desk_id="vet-g1", mint="M", symbol="M", leg="core", notional_usd=10,
            mark=Mark(0.01, 80_000, "test"), reason="rules", idempotency_key="q-1",
        )
