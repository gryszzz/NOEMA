"""One-shot, secret-bearing Solana signer boundary.

This module is launched as a short-lived child process by WalletSigner. Its
stdin and stdout carry only structured intents and public results. The private
key is fetched directly from macOS Keychain and never crosses the process
boundary.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
from decimal import Decimal
from typing import Any

import httpx

from .wallet_credentials import (
    load_bitcoin_private_key_in_signer_boundary,
    load_evm_account_in_signer_boundary,
    load_solana_keypair_in_signer_boundary,
)

EVM_CHAIN_IDS = {"ethereum": 1, "base": 8453, "polygon": 137}
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Deliberately false until the canonical mission-authority validator is wired
# into this credential-isolated process. Environment flags cannot enable it.
CANONICAL_LIVE_AUTHORITY_GATE_WIRED = False


def _rpc(client: httpx.Client, endpoint: str, method: str, params: list[Any]) -> Any:
    response = client.post(
        endpoint,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError("Solana RPC request failed")
    if "result" not in body:
        raise RuntimeError("Solana RPC response was incomplete")
    return body["result"]


def _public_keypair() -> Any:
    return load_solana_keypair_in_signer_boundary()


def inspect_wallet(request: dict[str, Any]) -> dict[str, Any]:
    keypair = _public_keypair()
    address = str(keypair.pubkey())
    configured = request.get("expected_address")
    if configured and configured != address:
        raise RuntimeError("derived wallet does not match configured wallet identity")
    return {"status": "credential_loaded", "address": address}


def inspect_evm_wallet() -> dict[str, Any]:
    account = load_evm_account_in_signer_boundary()
    return {"status": "credential_loaded", "address": account.address}


def inspect_bitcoin_wallet() -> dict[str, Any]:
    from embit import networks, script

    key = load_bitcoin_private_key_in_signer_boundary()
    address = script.p2wpkh(key.get_public_key()).address(networks.NETWORKS["main"])
    return {"status": "credential_loaded", "address": address}


def read_bitcoin_balance(request: dict[str, Any]) -> dict[str, Any]:
    from embit import networks, script

    key = load_bitcoin_private_key_in_signer_boundary()
    address = script.p2wpkh(key.get_public_key()).address(networks.NETWORKS["main"])
    if request.get("expected_address") and request["expected_address"] != address:
        raise RuntimeError("derived Bitcoin wallet does not match configured identity")
    endpoint = request.get("esplora_url", "https://blockstream.info/api")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise RuntimeError("Bitcoin read-only API endpoint is invalid")
    response = httpx.get(f"{endpoint.rstrip('/')}/address/{address}", timeout=15.0)
    response.raise_for_status()
    data = response.json()
    chain = data.get("chain_stats") or {}
    mempool = data.get("mempool_stats") or {}
    confirmed = int(chain.get("funded_txo_sum", 0)) - int(chain.get("spent_txo_sum", 0))
    unconfirmed = int(mempool.get("funded_txo_sum", 0)) - int(mempool.get("spent_txo_sum", 0))
    return {
        "status": "read_only_balance",
        "address": address,
        "confirmed_sats": confirmed,
        "unconfirmed_sats": unconfirmed,
        "total_sats": confirmed + unconfirmed,
        "btc": str(Decimal(confirmed + unconfirmed) / Decimal(100_000_000)),
    }


def _bitcoin_build(request: dict[str, Any]) -> tuple[Any, Any, int, int, str, str]:
    from embit import networks, script
    from embit.psbt import PSBT
    from embit.transaction import Transaction, TransactionInput, TransactionOutput

    key = load_bitcoin_private_key_in_signer_boundary()
    network = networks.NETWORKS["main"]
    address = script.p2wpkh(key.get_public_key()).address(network)
    if request.get("expected_address") and request["expected_address"] != address:
        raise RuntimeError("derived Bitcoin wallet does not match configured identity")
    endpoint = request.get("esplora_url", "https://blockstream.info/api")
    destination = request.get("destination")
    amount_sats = request.get("amount_sats")
    fee_limit = request.get("fee_limit_sats")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise RuntimeError("Bitcoin read-only API endpoint is invalid")
    if isinstance(amount_sats, bool) or not isinstance(amount_sats, int) or amount_sats <= 0:
        raise RuntimeError("Bitcoin transfer amount is invalid")
    if isinstance(fee_limit, bool) or not isinstance(fee_limit, int) or fee_limit < 0:
        raise RuntimeError("Bitcoin intent requires a nonnegative fee ceiling")
    try:
        destination_script = script.address_to_scriptpubkey(destination)
    except Exception:  # noqa: BLE001 - never return address parser input or detail
        raise RuntimeError("Bitcoin destination is invalid") from None

    with httpx.Client(timeout=15.0) as client:
        utxo_response = client.get(f"{endpoint.rstrip('/')}/address/{address}/utxo")
        utxo_response.raise_for_status()
        utxos = [
            item for item in utxo_response.json()
            if (item.get("status") or {}).get("confirmed") is True
        ]
        fee_response = client.get(f"{endpoint.rstrip('/')}/fee-estimates")
        fee_response.raise_for_status()
        fee_estimates = fee_response.json()

    if not utxos:
        raise RuntimeError("Bitcoin wallet has no confirmed spendable UTXOs")
    fee_rate = int(max(1, min(1000, float(fee_estimates.get("3", 2)) + 0.999)))
    selected: list[dict[str, Any]] = []
    total = 0
    for utxo in sorted(utxos, key=lambda row: int(row.get("value", 0)), reverse=True):
        selected.append(utxo)
        total += int(utxo["value"])
        estimated_vsize = 10 + len(selected) * 68 + 2 * 31
        if total >= amount_sats + estimated_vsize * fee_rate:
            break
    if total < amount_sats + (10 + len(selected) * 68 + 31) * fee_rate:
        raise RuntimeError("Bitcoin confirmed balance cannot cover amount and fee")

    change = total - amount_sats - (10 + len(selected) * 68 + 2 * 31) * fee_rate
    outputs = [TransactionOutput(amount_sats, destination_script)]
    if change >= 546:
        outputs.append(TransactionOutput(change, script.address_to_scriptpubkey(address)))
        fee = total - amount_sats - change
    else:
        fee = total - amount_sats
    if fee > fee_limit:
        raise RuntimeError("Bitcoin estimated fee exceeds intent limit")

    own_script = script.address_to_scriptpubkey(address)
    inputs = [
        TransactionInput(bytes.fromhex(row["txid"])[::-1], int(row["vout"]))
        for row in selected
    ]
    tx = Transaction(vin=inputs, vout=outputs)
    psbt = PSBT(tx)
    for index, row in enumerate(selected):
        psbt.inputs[index].witness_utxo = TransactionOutput(int(row["value"]), own_script)
    txid = tx.txid().hex()
    return tx, psbt, fee, fee_rate, address, txid


def simulate_bitcoin_transaction(request: dict[str, Any]) -> dict[str, Any]:
    _tx, _psbt, fee, fee_rate, address, txid = _bitcoin_build(request)
    return {
        "status": "constructed_preflight_passed",
        "chain": "bitcoin",
        "address": address,
        "destination": request["destination"],
        "amount_sats": request["amount_sats"],
        "estimated_fee_sats": fee,
        "fee_rate_sat_vbyte": fee_rate,
        "unsigned_txid": txid,
        "simulation": {"supported": False, "reason": "Bitcoin mainnet does not provide EVM-style eth_call simulation"},
    }


def execute_bitcoin_transaction(request: dict[str, Any]) -> dict[str, Any]:
    if not CANONICAL_LIVE_AUTHORITY_GATE_WIRED:
        raise RuntimeError("canonical wallet authority gate is not wired")
    if request.get("signer_enabled") is not True or request.get("master_halt") is not False:
        raise RuntimeError("Bitcoin signer disabled or owner master halt enabled")
    from embit.finalizer import finalize_psbt

    _tx, psbt, fee, fee_rate, address, _txid = _bitcoin_build(request)
    key = load_bitcoin_private_key_in_signer_boundary()
    if psbt.sign_with(key) < 1:
        raise RuntimeError("Bitcoin signer could not sign the constructed transaction")
    final_tx = finalize_psbt(psbt)
    if final_tx is None:
        raise RuntimeError("Bitcoin transaction finalization failed")
    raw_hex = final_tx.serialize().hex()
    txid = final_tx.txid().hex()
    endpoint = request.get("esplora_url", "https://blockstream.info/api")
    response = httpx.post(f"{endpoint.rstrip('/')}/tx", content=raw_hex, timeout=20.0)
    if response.is_error:
        raise RuntimeError("Bitcoin transaction broadcast failed")
    returned_txid = response.text.strip().strip('"')
    if returned_txid != txid:
        raise RuntimeError("Bitcoin broadcast transaction identity mismatch")
    confirmed = False
    for _ in range(40):
        status_response = httpx.get(f"{endpoint.rstrip('/')}/tx/{txid}/status", timeout=10.0)
        if status_response.is_success and status_response.json().get("confirmed") is True:
            confirmed = True
            break
        time.sleep(1)
    return {
        "status": "confirmed" if confirmed else "submitted",
        "chain": "bitcoin",
        "address": address,
        "transaction_hash": txid,
        "fee_paid_sats": fee,
        "fee_rate_sat_vbyte": fee_rate,
    }


def _evm_rpc(client: httpx.Client, endpoint: str, method: str, params: list[Any]) -> Any:
    try:
        response = client.post(
            endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise RuntimeError("EVM RPC request failed") from None
    if body.get("error") or "result" not in body:
        raise RuntimeError("EVM RPC request failed")
    return body["result"]


def evm_read_balances(request: dict[str, Any]) -> dict[str, Any]:
    account = load_evm_account_in_signer_boundary()
    address = account.address
    if request.get("expected_address") and request["expected_address"].lower() != address.lower():
        raise RuntimeError("derived EVM wallet does not match configured identity")
    chain = request.get("chain")
    expected_chain_id = EVM_CHAIN_IDS.get(chain)
    endpoint = request.get("rpc_url")
    if expected_chain_id is None or not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise RuntimeError("EVM chain or RPC endpoint is not configured")
    with httpx.Client(timeout=15.0) as client:
        chain_id = int(_evm_rpc(client, endpoint, "eth_chainId", []), 16)
        if chain_id != expected_chain_id:
            raise RuntimeError("EVM RPC chain ID does not match configured chain")
        balance_wei = int(_evm_rpc(client, endpoint, "eth_getBalance", [address, "latest"]), 16)
        tokens = []
        token_status = "configured_contracts_only"
        for token_address in request.get("tokens", []):
            if not isinstance(token_address, str) or not EVM_ADDRESS_RE.fullmatch(token_address):
                continue
            call_data = "0x70a08231" + address[2:].lower().rjust(64, "0")
            raw_balance = _evm_rpc(
                client,
                endpoint,
                "eth_call",
                [{"to": token_address, "data": call_data}, "latest"],
            )
            tokens.append({"contract": token_address, "raw_amount": str(int(raw_balance, 16))})
        indexer = request.get("token_indexer_url")
        if isinstance(indexer, str) and indexer.startswith("https://"):
            try:
                response = client.get(
                    f"{indexer.rstrip('/')}/addresses/{address}/token-balances"
                )
                response.raise_for_status()
                indexed = response.json()
                if not isinstance(indexed, list):
                    raise TypeError
                tokens = []
                for row in indexed[:100]:
                    token = row.get("token") or {}
                    tokens.append({
                        "contract": token.get("address_hash"),
                        "symbol": token.get("symbol"),
                        "decimals": token.get("decimals"),
                        "raw_amount": str(row.get("value", "0")),
                    })
                token_status = "indexed"
            except Exception:  # noqa: BLE001 - token indexer is best-effort public data
                token_status = "unavailable"
    return {
        "status": "read_only_balances",
        "chain": chain,
        "chain_id": chain_id,
        "address": address,
        "native_balance_wei": str(balance_wei),
        "native_balance": str(Decimal(balance_wei) / Decimal(10**18)),
        "tokens": tokens,
        "token_status": token_status,
    }


def _evm_transaction_parts(client: httpx.Client, request: dict[str, Any], account: Any) -> tuple[dict[str, Any], int, int, int]:
    chain = request.get("chain")
    expected_chain_id = EVM_CHAIN_IDS.get(chain)
    endpoint = request.get("rpc_url")
    if expected_chain_id is None or not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise RuntimeError("EVM chain or RPC endpoint is not configured")
    chain_id = int(_evm_rpc(client, endpoint, "eth_chainId", []), 16)
    if chain_id != expected_chain_id:
        raise RuntimeError("EVM RPC chain ID does not match configured chain")
    if request.get("expected_address", "").lower() != account.address.lower():
        raise RuntimeError("derived EVM wallet does not match configured identity")
    if request.get("source", "").lower() != account.address.lower():
        raise RuntimeError("EVM intent source does not match signer wallet")

    action = request.get("action")
    destination = request.get("destination")
    if not isinstance(destination, str) or not EVM_ADDRESS_RE.fullmatch(destination):
        raise RuntimeError("EVM destination is invalid")
    value = request.get("amount_wei", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("EVM transfer amount is invalid")
    if request.get("operational_validation") and (
        request.get("chain") != "base"
        or action != "evm_native_transfer"
        or value != 0
        or destination.lower() != account.address.lower()
    ):
        raise RuntimeError("operational validation is restricted to a zero-value Base self-call")
    if value == 0 and not request.get("operational_validation"):
        raise RuntimeError("zero-value EVM transfers are only allowed for operational validation")
    if action == "evm_native_transfer":
        tx_to, tx_value, data = destination, value, "0x"
    elif action == "evm_erc20_transfer":
        token_contract = request.get("contract_or_program")
        token_amount = request.get("amount_atomic")
        if not isinstance(token_contract, str) or not EVM_ADDRESS_RE.fullmatch(token_contract):
            raise RuntimeError("EVM token contract is invalid")
        if isinstance(token_amount, bool) or not isinstance(token_amount, int) or token_amount <= 0:
            raise RuntimeError("EVM token amount is invalid")
        data = "0xa9059cbb" + destination[2:].lower().rjust(64, "0") + format(token_amount, "064x")
        tx_to, tx_value = token_contract, 0
    else:
        raise RuntimeError("EVM signer received an unsupported structured action")

    transaction = {"from": account.address, "to": tx_to, "value": hex(tx_value), "data": data}
    _evm_rpc(client, endpoint, "eth_call", [transaction, "latest"])
    gas_limit = int(_evm_rpc(client, endpoint, "eth_estimateGas", [transaction]), 16)
    gas_price = int(_evm_rpc(client, endpoint, "eth_gasPrice", []), 16)
    native_balance = int(_evm_rpc(client, endpoint, "eth_getBalance", [account.address, "latest"]), 16)
    maximum_fee = gas_limit * gas_price
    if native_balance < value + maximum_fee:
        raise RuntimeError("EVM wallet balance cannot cover transfer and maximum fee")
    if action == "evm_erc20_transfer":
        token_balance_data = _evm_rpc(
            client,
            endpoint,
            "eth_call",
            [{
                "to": request["contract_or_program"],
                "data": "0x70a08231" + account.address[2:].lower().rjust(64, "0"),
            }, "latest"],
        )
        if int(token_balance_data, 16) < request["amount_atomic"]:
            raise RuntimeError("EVM token balance cannot cover transfer")
    return transaction, chain_id, gas_limit, gas_price


def simulate_evm_transaction(request: dict[str, Any]) -> dict[str, Any]:
    account = load_evm_account_in_signer_boundary()
    with httpx.Client(timeout=20.0) as client:
        _transaction, chain_id, gas_limit, gas_price = _evm_transaction_parts(client, request, account)
    fee_wei = gas_limit * gas_price
    fee_limit = request.get("fee_limit_wei")
    if isinstance(fee_limit, bool) or not isinstance(fee_limit, int) or fee_limit < 0:
        raise RuntimeError("EVM intent requires a nonnegative fee limit")
    if fee_wei > fee_limit:
        raise RuntimeError("EVM estimated fee exceeds intent limit")
    return {
        "status": "simulated",
        "chain": request["chain"],
        "chain_id": chain_id,
        "address": account.address,
        "destination": request["destination"],
        "gas_limit": gas_limit,
        "gas_price_wei": str(gas_price),
        "maximum_fee_wei": str(fee_wei),
        "simulation": {"ok": True, "method": "eth_call+eth_estimateGas"},
    }


def sign_dry_run_evm_transaction(request: dict[str, Any]) -> dict[str, Any]:
    account = load_evm_account_in_signer_boundary()
    endpoint = request.get("rpc_url")
    with httpx.Client(timeout=20.0) as client:
        transaction, chain_id, gas_limit, gas_price = _evm_transaction_parts(client, request, account)
        fee_limit = request.get("fee_limit_wei")
        if isinstance(fee_limit, bool) or not isinstance(fee_limit, int) or fee_limit < 0:
            raise RuntimeError("EVM intent requires a nonnegative fee limit")
        if gas_limit * gas_price > fee_limit:
            raise RuntimeError("EVM estimated fee exceeds intent limit")
        nonce = int(_evm_rpc(client, endpoint, "eth_getTransactionCount", [account.address, "pending"]), 16)
    tx = {
        "chainId": chain_id,
        "nonce": nonce,
        "to": transaction["to"],
        "value": int(transaction["value"], 16),
        "data": transaction["data"],
        "gas": gas_limit,
        "gasPrice": gas_price,
    }
    signed = account.sign_transaction(tx)
    return {
        "status": "signed_not_broadcast",
        "chain": request["chain"],
        "chain_id": chain_id,
        "address": account.address,
        "signing_verified": bool(signed.raw_transaction),
        "transaction_hash": "0x" + signed.hash.hex(),
        "gas_limit": gas_limit,
        "estimated_fee_wei": str(gas_limit * gas_price),
    }


def execute_evm_transaction(request: dict[str, Any]) -> dict[str, Any]:
    if not CANONICAL_LIVE_AUTHORITY_GATE_WIRED:
        raise RuntimeError("canonical wallet authority gate is not wired")
    if request.get("signer_enabled") is not True or request.get("master_halt") is not False:
        raise RuntimeError("EVM signer disabled or owner master halt enabled")
    account = load_evm_account_in_signer_boundary()
    endpoint = request.get("rpc_url")
    with httpx.Client(timeout=25.0) as client:
        transaction, chain_id, gas_limit, gas_price = _evm_transaction_parts(client, request, account)
        fee_wei = gas_limit * gas_price
        fee_limit = request.get("fee_limit_wei")
        if isinstance(fee_limit, bool) or not isinstance(fee_limit, int) or fee_limit < 0:
            raise RuntimeError("EVM intent requires a nonnegative fee limit")
        if fee_wei > fee_limit:
            raise RuntimeError("EVM estimated fee exceeds intent limit")
        nonce = int(_evm_rpc(client, endpoint, "eth_getTransactionCount", [account.address, "pending"]), 16)
        tx = {
            "chainId": chain_id,
            "nonce": nonce,
            "to": transaction["to"],
            "value": int(transaction["value"], 16),
            "data": transaction["data"],
            "gas": gas_limit,
            "gasPrice": gas_price,
        }
        signed = account.sign_transaction(tx)
        raw = "0x" + bytes(signed.raw_transaction).hex()
        tx_hash = _evm_rpc(client, endpoint, "eth_sendRawTransaction", [raw])
        receipt = None
        for _ in range(40):
            receipt = _evm_rpc(client, endpoint, "eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                break
            time.sleep(1)
        if receipt is None:
            return {"status": "submitted", "chain": request["chain"], "chain_id": chain_id,
                    "address": account.address, "transaction_hash": tx_hash}
        receipt_status = int(receipt.get("status", "0x0"), 16)
        gas_used = int(receipt.get("gasUsed", "0x0"), 16)
        effective_gas_price = int(receipt.get("effectiveGasPrice", hex(gas_price)), 16)
        reconciled_fee = gas_used * effective_gas_price
        post_balance = int(_evm_rpc(client, endpoint, "eth_getBalance", [account.address, "latest"]), 16)
    return {
        "status": "confirmed" if receipt_status == 1 else "reverted",
        "chain": request["chain"],
        "chain_id": chain_id,
        "address": account.address,
        "transaction_hash": tx_hash,
        "block_number": int(receipt.get("blockNumber", "0x0"), 16),
        "gas_used": gas_used,
        "effective_gas_price_wei": str(effective_gas_price),
        "fee_paid_wei": str(reconciled_fee),
        "post_balance_wei": str(post_balance),
    }


def read_balances(request: dict[str, Any]) -> dict[str, Any]:
    keypair = _public_keypair()
    address = str(keypair.pubkey())
    configured = request.get("expected_address")
    if configured and configured != address:
        raise RuntimeError("derived wallet does not match configured wallet identity")

    endpoint = request.get("rpc_url")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise RuntimeError("Solana RPC endpoint is not configured")

    with httpx.Client(timeout=12.0) as client:
        lamports_result = _rpc(
            client,
            endpoint,
            "getBalance",
            [address, {"commitment": "confirmed"}],
        )
        accounts_result = _rpc(
            client,
            endpoint,
            "getTokenAccountsByOwner",
            [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )

    lamports = int(lamports_result["value"])
    token_accounts = accounts_result.get("value") or []
    tokens: list[dict[str, Any]] = []
    for account in token_accounts:
        parsed = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        token_amount = parsed.get("tokenAmount", {})
        if not parsed.get("mint") or not isinstance(token_amount.get("amount"), str):
            continue
        tokens.append(
            {
                "mint": str(parsed["mint"]),
                "raw_amount": str(token_amount["amount"]),
                "decimals": int(token_amount.get("decimals", 0)),
                "ui_amount": token_amount.get("uiAmountString"),
            }
        )
    return {
        "status": "read_only_balances",
        "address": address,
        "sol_lamports": lamports,
        "sol": str(Decimal(lamports) / Decimal(1_000_000_000)),
        "tokens": tokens,
    }


def _native_transfer_transaction(
    client: httpx.Client,
    endpoint: str,
    source: Any,
    destination: Any,
    lamports: int,
) -> tuple[Any, Any, int]:
    from solders.hash import Hash
    from solders.message import Message
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction

    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RuntimeError("invalid native transfer amount")
    latest = _rpc(client, endpoint, "getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash = Hash.from_string(latest["value"]["blockhash"])
    instruction = transfer(
        TransferParams(from_pubkey=source, to_pubkey=destination, lamports=lamports)
    )
    message = Message.new_with_blockhash([instruction], source, blockhash)
    transaction = Transaction.new_unsigned(message)
    fee_data = _rpc(
        client,
        endpoint,
        "getFeeForMessage",
        [base64.b64encode(bytes(transaction.message)).decode("ascii"), {"commitment": "confirmed"}],
    )
    fee_lamports = int(fee_data["value"])
    return transaction, blockhash, fee_lamports


def _simulate_native_transfer(
    client: httpx.Client,
    endpoint: str,
    transaction: Any,
) -> dict[str, Any]:
    encoded = base64.b64encode(bytes(transaction)).decode("ascii")
    simulation = _rpc(
        client,
        endpoint,
        "simulateTransaction",
        [encoded, {"encoding": "base64", "commitment": "confirmed", "sigVerify": False, "replaceRecentBlockhash": True}],
    )["value"]
    err = simulation.get("err")
    return {
        "ok": err is None,
        "error": None if err is None else "transaction simulation rejected",
        "units_consumed": simulation.get("unitsConsumed"),
    }


def simulate_native_transfer(request: dict[str, Any]) -> dict[str, Any]:
    from solders.pubkey import Pubkey

    keypair = _public_keypair()
    source = keypair.pubkey()
    address = str(source)
    if request.get("expected_address") and request["expected_address"] != address:
        raise RuntimeError("derived wallet does not match configured wallet identity")
    if request.get("source") != address:
        raise RuntimeError("intent source does not match signer wallet")
    destination = Pubkey.from_string(str(request.get("destination", "")))
    endpoint = request.get("rpc_url")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise RuntimeError("Solana RPC endpoint is not configured")
    with httpx.Client(timeout=15.0) as client:
        transaction, _blockhash, fee_lamports = _native_transfer_transaction(
            client, endpoint, source, destination, request.get("amount_lamports")
        )
        simulation = _simulate_native_transfer(client, endpoint, transaction)
    return {
        "status": "simulated" if simulation["ok"] else "simulation_rejected",
        "address": address,
        "destination": str(destination),
        "amount_lamports": request["amount_lamports"],
        "fee_lamports": fee_lamports,
        "simulation": simulation,
    }


def sign_dry_run_solana_transfer(request: dict[str, Any]) -> dict[str, Any]:
    from solders.pubkey import Pubkey

    keypair = _public_keypair()
    source = keypair.pubkey()
    address = str(source)
    if request.get("expected_address") and request["expected_address"] != address:
        raise RuntimeError("derived wallet does not match configured wallet identity")
    if request.get("source") != address:
        raise RuntimeError("intent source does not match signer wallet")
    destination = Pubkey.from_string(str(request.get("destination", "")))
    endpoint = request.get("rpc_url")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise RuntimeError("Solana RPC endpoint is not configured")
    with httpx.Client(timeout=15.0) as client:
        transaction, blockhash, fee_lamports = _native_transfer_transaction(
            client, endpoint, source, destination, request.get("amount_lamports")
        )
        simulation = _simulate_native_transfer(client, endpoint, transaction)
    if not simulation["ok"]:
        raise RuntimeError("transaction simulation rejected")
    fee_limit = request.get("fee_limit_lamports")
    if isinstance(fee_limit, bool) or not isinstance(fee_limit, int) or fee_lamports > fee_limit:
        raise RuntimeError("network fee exceeds intent limit")
    transaction.sign([keypair], blockhash)
    return {
        "status": "signed_not_broadcast",
        "address": address,
        "signing_verified": transaction.is_signed(),
        "fee_lamports": fee_lamports,
        "simulation": simulation,
    }


def execute_native_transfer(request: dict[str, Any]) -> dict[str, Any]:
    if not CANONICAL_LIVE_AUTHORITY_GATE_WIRED:
        raise RuntimeError("canonical wallet authority gate is not wired")
    from solders.pubkey import Pubkey

    if request.get("signer_enabled") is not True or request.get("master_halt") is not False:
        raise RuntimeError("signer disabled or owner master halt enabled")
    keypair = _public_keypair()
    source = keypair.pubkey()
    address = str(source)
    if request.get("expected_address") and request["expected_address"] != address:
        raise RuntimeError("derived wallet does not match configured wallet identity")
    if request.get("source") != address:
        raise RuntimeError("intent source does not match signer wallet")
    destination = Pubkey.from_string(str(request.get("destination", "")))
    endpoint = request.get("rpc_url")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise RuntimeError("Solana RPC endpoint is not configured")
    with httpx.Client(timeout=20.0) as client:
        transaction, blockhash, fee_lamports = _native_transfer_transaction(
            client, endpoint, source, destination, request.get("amount_lamports")
        )
        fee_limit = request.get("fee_limit_lamports")
        if isinstance(fee_limit, bool) or not isinstance(fee_limit, int) or fee_limit < 0:
            raise RuntimeError("invalid fee limit")
        if fee_lamports > fee_limit:
            raise RuntimeError("network fee exceeds intent limit")
        simulation = _simulate_native_transfer(client, endpoint, transaction)
        if not simulation["ok"]:
            raise RuntimeError("transaction simulation rejected")

        transaction.sign([keypair], blockhash)
        encoded = base64.b64encode(bytes(transaction)).decode("ascii")
        signature = _rpc(
            client,
            endpoint,
            "sendTransaction",
            [encoded, {"encoding": "base64", "preflightCommitment": "confirmed", "skipPreflight": False}],
        )
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("transaction broadcast returned no signature")
        confirmed = False
        for _ in range(30):
            statuses = _rpc(client, endpoint, "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            values = statuses.get("value") or []
            status = values[0] if values else None
            if status and status.get("err"):
                raise RuntimeError("broadcast transaction failed on chain")
            if status and status.get("confirmationStatus") in {"confirmed", "finalized"}:
                confirmed = True
                break
            time.sleep(1)
        if not confirmed:
            raise RuntimeError("broadcast transaction confirmation timed out")
        transaction_result = _rpc(
            client,
            endpoint,
            "getTransaction",
            [signature, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
        )
    meta = transaction_result.get("meta") or {}
    return {
        "status": "confirmed",
        "address": address,
        "destination": str(destination),
        "signature": signature,
        "slot": transaction_result.get("slot"),
        "fee_lamports": int(meta.get("fee", fee_lamports)),
        "pre_balance_lamports": int((meta.get("preBalances") or [0])[0]),
        "post_balance_lamports": int((meta.get("postBalances") or [0])[0]),
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read())
        if not isinstance(request, dict):
            raise TypeError("signer request must be an object")
        operation = request.get("operation")
        if operation == "inspect":
            result = inspect_wallet(request)
        elif operation == "evm_inspect":
            result = inspect_evm_wallet()
        elif operation == "bitcoin_inspect":
            result = inspect_bitcoin_wallet()
        elif operation == "bitcoin_balance":
            result = read_bitcoin_balance(request)
        elif operation == "bitcoin_simulate_transaction":
            result = simulate_bitcoin_transaction(request)
        elif operation == "bitcoin_execute_transaction":
            result = execute_bitcoin_transaction(request)
        elif operation == "evm_balances":
            result = evm_read_balances(request)
        elif operation == "evm_simulate_transaction":
            result = simulate_evm_transaction(request)
        elif operation == "evm_sign_dry_run":
            result = sign_dry_run_evm_transaction(request)
        elif operation == "evm_execute_transaction":
            result = execute_evm_transaction(request)
        elif operation == "balances":
            result = read_balances(request)
        elif operation == "simulate_native_transfer":
            result = simulate_native_transfer(request)
        elif operation == "solana_sign_dry_run":
            result = sign_dry_run_solana_transfer(request)
        elif operation == "execute_native_transfer":
            result = execute_native_transfer(request)
        else:
            raise RuntimeError("unsupported signer operation")
        sys.stdout.write(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as exc:  # noqa: BLE001 - errors crossing this boundary are allowlisted
        # Only sanitized error classes/messages are emitted. Never include
        # request data, RPC endpoint, credentials, or chained subprocess output.
        message = str(exc)
        safe_messages = {
            "signer credential access failed",
            "signer credential is not a valid Solana keypair",
            "local Solana signer is supported only on macOS",
            "derived wallet does not match configured wallet identity",
            "Solana RPC endpoint is not configured",
            "Solana RPC request failed",
            "Solana RPC response was incomplete",
            "signer request must be an object",
            "unsupported signer operation",
            "EVM signer credential access failed",
            "EVM signer credential is not a valid private key",
            "Bitcoin signer credential access failed",
            "Bitcoin signer credential is not a supported mainnet key",
            "derived Bitcoin wallet does not match configured identity",
            "Bitcoin read-only API endpoint is invalid",
            "Bitcoin transfer amount is invalid",
            "Bitcoin destination is invalid",
            "Bitcoin intent requires a nonnegative fee ceiling",
            "Bitcoin wallet has no confirmed spendable UTXOs",
            "Bitcoin confirmed balance cannot cover amount and fee",
            "Bitcoin estimated fee exceeds intent limit",
            "Bitcoin signer disabled or owner master halt enabled",
            "Bitcoin signer could not sign the constructed transaction",
            "Bitcoin transaction finalization failed",
            "Bitcoin transaction broadcast failed",
            "Bitcoin broadcast transaction identity mismatch",
            "local Bitcoin signer is supported only on macOS",
            "local EVM signer is supported only on macOS",
            "derived EVM wallet does not match configured identity",
            "EVM chain or RPC endpoint is not configured",
            "EVM RPC chain ID does not match configured chain",
            "EVM RPC request failed",
            "EVM intent source does not match signer wallet",
            "EVM destination is invalid",
            "EVM transfer amount is invalid",
            "EVM transfer requires a mission and evidence references",
            "EVM transfer intent is incomplete",
            "operational validation is restricted to a zero-value Base self-call",
            "zero-value EVM transfers are only allowed for operational validation",
            "EVM token contract is invalid",
            "EVM token amount is invalid",
            "EVM signer received an unsupported structured action",
            "EVM intent requires a nonnegative fee limit",
            "EVM estimated fee exceeds intent limit",
            "EVM signer disabled or owner master halt enabled",
            "EVM wallet balance cannot cover transfer and maximum fee",
            "EVM token balance cannot cover transfer",
            "signer disabled or owner master halt enabled",
            "intent source does not match signer wallet",
            "invalid native transfer amount",
            "invalid fee limit",
            "network fee exceeds intent limit",
            "transaction simulation rejected",
            "broadcast transaction failed on chain",
            "broadcast transaction confirmation timed out",
            "broadcast transaction returned no signature",
            "canonical wallet authority gate is not wired",
        }
        if message not in safe_messages:
            message = "signer operation failed"
        sys.stdout.write(json.dumps({"error": message}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
