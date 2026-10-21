"""Sign one EVM native/ERC-20 swap inside the isolated signer process.

The parent sends chain, side, token, amount, slippage, minimum out, and an
idempotency key. This module quotes the public KyberSwap aggregator, pins the
router, simulates, signs, and submits. ``NOEMA_EVM_SIGNER_SECRET`` is read only
by the caller in the signer process and is never logged or returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

# The only address this swap signer will sign for. A matching env value is
# required as well; a different derived key is refused.
EXPECTED_EVM_SIGNER_ADDRESS = "0x92B9a0C66a37fE9635e55045b3bf40cB035a59C8"
KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
KYBER_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
DEFAULT_AGGREGATOR_URL = "https://aggregator-api.kyberswap.com"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_WEI = Decimal(10**18)
_LIVE_LEVELS = frozenset({"micro", "proven", "self_funded", "expansion"})

REQUEST_FIELDS = (
    "chain", "side", "token", "amount", "max_slippage_bps", "min_out", "idempotency_key",
)
PUBLIC_RESULT_FIELDS = (
    "status", "transaction_hash", "address", "chain", "chain_id", "token", "side",
    "amount_in", "amount_out", "min_out", "fee_wei", "native_delta_wei", "token_delta_raw",
    "token_decimals", "block_number", "idempotency_key", "notional_usd", "native_price_usd",
    "price_timestamp",
)


@dataclass(frozen=True)
class EvmSwapChain:
    """One swap-capable network. Add a chain here when its router is pinned.

    Robinhood Chain (chain id 4663) is included so a later route does not need a
    new signer shape. It still fails closed until its RPC, price, venue, and
    token allowlist are set. There is no default RPC.
    """

    name: str
    chain_id: int
    native_symbol: str
    aggregator_slug: str
    router: str
    wrapped_native: str | None = None


SWAP_CHAINS: dict[str, EvmSwapChain] = {
    "ethereum": EvmSwapChain(
        "ethereum", 1, "ETH", "ethereum", KYBER_ROUTER,
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    ),
    "base": EvmSwapChain(
        "base", 8453, "ETH", "base", KYBER_ROUTER,
        "0x4200000000000000000000000000000000000006",
    ),
    "polygon": EvmSwapChain(
        "polygon", 137, "POL", "polygon", KYBER_ROUTER,
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    ),
    "robinhood": EvmSwapChain("robinhood", 4663, "ETH", "robinhood", KYBER_ROUTER),
}


def public_swap_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in PUBLIC_RESULT_FIELDS if key in payload}


def perform_evm_swap(request: dict[str, Any], account: Any, *,
                     client: httpx.Client | None = None) -> dict[str, Any]:
    """Quote, simulate, sign, send, and confirm one allowlisted native/ERC-20 swap."""
    fields = _parse_request(request)
    chain = SWAP_CHAINS[fields["chain"]]
    _require_gates(chain.name)
    address = _require_signer(account)
    price = _native_price(chain.name)
    per_trade, daily_cap = resolve_usd_caps()
    rpc_url = _rpc_url(chain)
    aggregator = _endpoint(
        os.getenv("NOEMA_KYBERSWAP_API_URL", ""), DEFAULT_AGGREGATOR_URL,
        missing="aggregator api endpoint is not configured",
    )
    owned = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        return _perform(
            fields, chain, account, address, client,
            per_trade=per_trade, daily_cap=daily_cap, price=price,
            rpc_url=rpc_url, aggregator=aggregator,
        )
    finally:
        if owned:
            client.close()


def resolve_usd_caps() -> tuple[Decimal, Decimal]:
    """Per-trade and daily USD caps. Authority fractions win when the file exists."""
    path = _authority_path()
    env_trade = _env_decimal("NOEMA_EVM_MAX_TRADE_USD")
    env_daily = _env_decimal("NOEMA_EVM_MAX_DAILY_USD")
    if path is None:
        if env_trade is None or env_daily is None:
            raise RuntimeError("swap caps are not configured")
        return env_trade, env_daily
    import tomllib

    level = os.getenv("NOEMA_AUTONOMY_LEVEL", "").strip().lower()
    bankroll = _env_decimal("NOEMA_EVM_BANKROLL_USD")
    if level not in _LIVE_LEVELS or bankroll is None:
        raise RuntimeError("swap caps are not configured" if not level or bankroll is None
                           else "autonomy level does not permit live swaps")
    try:
        with path.open("rb") as handle:
            table = tomllib.load(handle)
        section = table.get(level)
        if not isinstance(section, dict) or section.get("live") is not True:
            raise RuntimeError("autonomy level does not permit live swaps")
        per = Decimal(str(section["per_trade_fraction"]))
        daily = Decimal(str(section["daily_notional_fraction"]))
    except RuntimeError:
        raise
    except (OSError, KeyError, TypeError, InvalidOperation, tomllib.TOMLDecodeError):
        raise RuntimeError("swap caps are not configured") from None
    if not per.is_finite() or not daily.is_finite() or not (0 < per <= 1) or not (0 < daily <= 1):
        raise RuntimeError("swap caps are not configured")
    per_cap, daily_cap = bankroll * per, bankroll * daily
    if env_trade is not None:
        per_cap = min(per_cap, env_trade)
    if env_daily is not None:
        daily_cap = min(daily_cap, env_daily)
    if per_cap <= 0 or daily_cap <= 0:
        raise RuntimeError("swap caps are not configured")
    return per_cap, daily_cap


def _perform(fields: dict[str, Any], chain: EvmSwapChain, account: Any, address: str,
             client: httpx.Client, *, per_trade: Decimal, daily_cap: Decimal, price: Decimal,
             rpc_url: str, aggregator: str) -> dict[str, Any]:
    digest = _request_hash({key: fields[key] for key in REQUEST_FIELDS})
    conn = _connect()
    try:
        resumed = _claim(conn, fields["idempotency_key"], digest)
        if isinstance(resumed, dict):
            return resumed
        if isinstance(resumed, _Resume):
            raw_hex = resumed.raw_tx
            tx_hash = resumed.tx_hash
            notional = Decimal(resumed.notional_usd)
            day = resumed.day
            pre_balance = int(resumed.pre_balance_wei)
            value_wei = int(resumed.value_wei)
            decimals = int(resumed.token_decimals)
        else:
            _reject_listed_token(chain, fields["token"])
            _assert_chain_id(client, rpc_url, chain.chain_id)
            decimals = _token_metadata(client, rpc_url, fields["token"])
            pre_balance = _balance(client, rpc_url, address)
            value_wei = fields["amount"] if fields["side"] == "buy" else 0
            if fields["side"] == "sell":
                _require_allowance(client, rpc_url, fields["token"], address, fields["amount"])
                _require_token_balance(client, rpc_url, fields["token"], address, fields["amount"])
            if fields["side"] == "buy":
                _enforce_cap(_usd(fields["amount"], price), per_trade, daily_cap, Decimal(0))
            quote = _route(client, aggregator, chain, fields, address)
            if fields["side"] == "sell":
                _enforce_cap(_usd(int(quote["amountOut"]), price), per_trade, daily_cap, Decimal(0))
            built = _build(client, aggregator, chain, fields, address, quote)
            gas_price = _gas_price(client, rpc_url)
            gas_limit = _simulate(client, rpc_url, address, chain.router, built["data"], value_wei)
            fee_cap = gas_limit * gas_price
            _fee_within_cap(fee_cap)
            reserve = _env_int("NOEMA_EVM_FEE_RESERVE_WEI", 5_000_000_000_000_000,
                               minimum=0, maximum=10**24)
            if pre_balance < value_wei + fee_cap + reserve:
                raise RuntimeError("evm reserve would be breached")
            nonce = _quantity(client, rpc_url, "eth_getTransactionCount", [address, "pending"])
            raw_hex, tx_hash = _sign(
                account, chain_id=chain.chain_id, nonce=nonce, router=chain.router,
                data=built["data"], value_wei=value_wei, gas_limit=gas_limit, gas_price=gas_price,
            )
            notional = _usd(
                fields["amount"] if fields["side"] == "buy" else int(quote["amountOut"]), price,
            )
            day = datetime.now(UTC).date().isoformat()
            stored = _store_signed(
                conn, fields["idempotency_key"], raw_hex, tx_hash, notional, day,
                pre_balance=pre_balance, value_wei=value_wei, decimals=decimals,
                per_trade=per_trade, daily_cap=daily_cap,
            )
            if stored.get("status") == "confirmed":
                return public_swap_result(stored)
            raw_hex = str(stored["raw_tx"])
            tx_hash = str(stored["tx_hash"])
            notional = Decimal(str(stored["notional_usd"]))
            day = str(stored["day"])
            pre_balance = int(stored["pre_balance_wei"])
            value_wei = int(stored["value_wei"])
            decimals = int(stored["token_decimals"])
        try:
            sent = _rpc(client, rpc_url, "eth_sendRawTransaction", [raw_hex])
            if not isinstance(sent, str) or sent.lower() != tx_hash.lower():
                raise RuntimeError("broadcast transaction identity mismatch")
            receipt = _confirm(client, rpc_url, tx_hash)
        except RuntimeError as exc:
            if str(exc) == "broadcast transaction failed on chain":
                _remember_failure(conn, fields["idempotency_key"], str(exc), release=notional, day=day)
            raise
        post_balance = _balance(client, rpc_url, address) if fields["side"] == "sell" else pre_balance
        fill = _reconcile(
            receipt, side=fields["side"], wallet=address, token=fields["token"],
            value_wei=value_wei, pre_balance=pre_balance, post_balance=post_balance,
            min_out=fields["min_out"],
        )
        result = {
            **(fill or {}),
            "transaction_hash": tx_hash,
            "address": address,
            "chain": chain.name,
            "chain_id": chain.chain_id,
            "token": fields["token"],
            "side": fields["side"],
            "min_out": str(fields["min_out"]),
            "token_decimals": decimals,
            "idempotency_key": fields["idempotency_key"],
            "notional_usd": _money(notional),
            "native_price_usd": _money(price),
            "price_timestamp": _now(),
        }
        if fill is None:
            result["status"] = "failed"
        public = public_swap_result(result)
        terminal = "confirmed" if public.get("status") == "confirmed" else "failed"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE evm_swap_requests
               SET status=?, result_json=?, raw_tx=NULL, error=?, updated_at=?
               WHERE idempotency_key=?""",
            (
                terminal, json.dumps(public, sort_keys=True),
                None if terminal == "confirmed" else "swap fill could not be reconciled",
                _now(), fields["idempotency_key"],
            ),
        )
        if terminal != "confirmed":
            _add_daily(conn, day, -notional)
        conn.commit()
        if terminal != "confirmed":
            raise RuntimeError("swap fill could not be reconciled")
        return public
    finally:
        conn.close()


def _parse_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RuntimeError("swap request is invalid")  # noqa: TRY004
    chain_name = request.get("chain")
    side = request.get("side")
    token = request.get("token")
    amount = request.get("amount")
    slippage = request.get("max_slippage_bps")
    minimum = request.get("min_out")
    key = request.get("idempotency_key")
    if not isinstance(chain_name, str) or chain_name not in SWAP_CHAINS:
        raise RuntimeError("evm chain is not configured")
    if side not in {"buy", "sell"}:
        raise RuntimeError("swap side is not allowed")
    if not isinstance(token, str) or not _address_ok(token):
        raise RuntimeError("token contract is invalid")
    token = _checksum(token)
    chain = SWAP_CHAINS[chain_name]
    banned = {KYBER_NATIVE.lower(), KYBER_ROUTER.lower()}
    if chain.wrapped_native:
        banned.add(chain.wrapped_native.lower())
    if token.lower() in banned:
        raise RuntimeError("token contract is invalid")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount < 2**256:
        raise RuntimeError("swap request is invalid")
    if isinstance(slippage, bool) or not isinstance(slippage, int):
        raise RuntimeError("slippage exceeds signer maximum")  # noqa: TRY004
    configured = _env_int("NOEMA_EVM_MAX_SLIPPAGE_BPS", 100, minimum=1, maximum=2_000)
    if slippage < 1 or slippage > configured:
        raise RuntimeError("slippage exceeds signer maximum")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
        raise RuntimeError("minimum output is invalid")
    if not isinstance(key, str) or not 4 <= len(key) <= 200:
        raise RuntimeError("idempotency key is invalid")
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:._-+"
           for char in key):
        raise RuntimeError("idempotency key is invalid")
    return {
        "chain": chain_name, "side": side, "token": token, "amount": amount,
        "max_slippage_bps": slippage, "min_out": minimum, "idempotency_key": key,
    }


def _require_gates(chain_name: str) -> None:
    if (os.getenv("NOEMA_EVM_SIGNER_ENABLED", "0") != "1"
            or os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") != "1"):
        raise RuntimeError("evm live swaps are disabled")
    if os.getenv("NOEMA_EVM_OWNER_REVIEWED", "0") != "1":
        raise RuntimeError("evm owner review is required")
    venues = {
        item.strip().lower()
        for item in os.getenv("NOEMA_LIVE_VENUES", "").split(",")
        if item.strip()
    }
    if "evm" not in venues and chain_name not in venues:
        raise RuntimeError("evm venue is not allowlisted")
    if os.getenv("NOEMA_MASTER_HALT", "0") == "1":
        raise RuntimeError("owner master halt is active")
    if os.getenv("NOEMA_EVM_MASTER_HALT", "1") != "0":
        raise RuntimeError("evm master halt is active")
    if os.getenv("NOEMA_WALLET_MASTER_HALT", "1") != "0":
        raise RuntimeError("wallet master halt is active")


def _require_signer(account: Any) -> str:
    address = str(getattr(account, "address", "") or "")
    if not _address_ok(address):
        raise RuntimeError("configured evm wallet does not match signer")
    expected = EXPECTED_EVM_SIGNER_ADDRESS.lower()
    configured = os.getenv("NOEMA_EVM_ADDRESS", "").strip().lower()
    if address.lower() != expected or (configured and configured != expected):
        raise RuntimeError("configured evm wallet does not match signer")
    if not configured:
        raise RuntimeError("configured evm wallet does not match signer")
    return _checksum(address)


def _reject_listed_token(chain: EvmSwapChain, token: str) -> None:
    item = (chain.name, token.lower())
    if item in _token_set("NOEMA_EVM_TOKEN_DENYLIST"):
        raise RuntimeError("token is denylisted")
    path = os.getenv("NOEMA_EVM_TOKEN_DENYLIST_PATH", "").strip()
    file_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "evm_token_denylist.txt"
    if file_path.is_file() and item in _tokens_from_file(file_path):
        raise RuntimeError("token is denylisted")
    allowed = _token_set("NOEMA_EVM_TOKEN_ALLOWLIST")
    if item not in allowed:
        raise RuntimeError("token is not allowlisted")


def _route(client: httpx.Client, base: str, chain: EvmSwapChain, fields: dict[str, Any],
           address: str) -> dict[str, Any]:
    del address
    sell = KYBER_NATIVE if fields["side"] == "buy" else fields["token"]
    buy = fields["token"] if fields["side"] == "buy" else KYBER_NATIVE
    try:
        response = client.get(
            f"{base}/{chain.aggregator_slug}/api/v1/routes",
            params={"tokenIn": sell, "tokenOut": buy, "amountIn": str(fields["amount"])},
            headers=_aggregator_headers(),
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        raise RuntimeError("aggregator quote failed") from None
    if not isinstance(body, dict) or body.get("code") != 0:
        raise RuntimeError("aggregator quote failed")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    summary = data.get("routeSummary") if isinstance(data.get("routeSummary"), dict) else None
    router = str(data.get("routerAddress") or "")
    if summary is None or router.lower() != chain.router.lower():
        raise RuntimeError("swap router is not allowlisted")
    if (str(summary.get("tokenIn", "")).lower() != sell.lower()
            or str(summary.get("tokenOut", "")).lower() != buy.lower()
            or str(summary.get("amountIn")) != str(fields["amount"])):
        raise RuntimeError("quote does not match the requested swap")
    try:
        amount_out = int(summary["amountOut"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("aggregator quote failed") from None
    if amount_out < fields["min_out"] or amount_out <= 0:
        raise RuntimeError("quote is below minimum output")
    summary["amountOut"] = str(amount_out)
    return summary


def _build(client: httpx.Client, base: str, chain: EvmSwapChain, fields: dict[str, Any],
           address: str, summary: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.post(
            f"{base}/{chain.aggregator_slug}/api/v1/route/build",
            json={
                "routeSummary": summary,
                "sender": address,
                "recipient": address,
                "slippageTolerance": fields["max_slippage_bps"],
            },
            headers=_aggregator_headers(),
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        raise RuntimeError("aggregator swap build failed") from None
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None
    if data is None or body.get("code") != 0:
        raise RuntimeError("aggregator swap build failed")
    router = str(data.get("routerAddress") or "")
    calldata = data.get("data")
    try:
        value = int(data.get("transactionValue") or 0)
        amount_out = int(data["amountOut"])
    except (TypeError, ValueError):
        raise RuntimeError("aggregator swap build failed") from None
    expected_value = fields["amount"] if fields["side"] == "buy" else 0
    if router.lower() != chain.router.lower() or value != expected_value or amount_out < fields["min_out"]:
        raise RuntimeError("swap router is not allowlisted" if router.lower() != chain.router.lower()
                           else "quote does not match the requested swap")
    if not isinstance(calldata, str) or not calldata.startswith("0x") or len(calldata) < 10:
        raise RuntimeError("aggregator swap build failed")
    if len(calldata) > 200_000 or _address_word(address) not in calldata.lower():
        raise RuntimeError("aggregator swap build failed")
    try:
        bytes.fromhex(calldata[2:])
    except ValueError:
        raise RuntimeError("aggregator swap build failed") from None
    return {"data": calldata, "value": value, "amountOut": amount_out}


def _simulate(client: httpx.Client, rpc_url: str, address: str, router: str,
              data: str, value_wei: int) -> int:
    call = {"from": address, "to": router, "data": data, "value": hex(value_wei)}
    try:
        _rpc(client, rpc_url, "eth_call", [call, "latest"])
        estimated = _rpc(client, rpc_url, "eth_estimateGas", [call, "latest"])
        gas = int(estimated, 16)
    except (RuntimeError, TypeError, ValueError):
        raise RuntimeError("transaction simulation rejected") from None
    if not 21_000 <= gas <= 8_000_000:
        raise RuntimeError("transaction simulation rejected")
    return min(gas * 12 // 10, 8_000_000)


def _sign(account: Any, *, chain_id: int, nonce: int, router: str, data: str,
          value_wei: int, gas_limit: int, gas_price: int) -> tuple[str, str]:
    signed = account.sign_transaction({
        "chainId": chain_id,
        "nonce": nonce,
        "to": router,
        "value": value_wei,
        "data": data,
        "gas": gas_limit,
        "gasPrice": gas_price,
    })
    raw = bytes(signed.raw_transaction)
    digest = bytes(signed.hash)
    if not raw or len(digest) != 32:
        raise RuntimeError("aggregator swap build failed")
    return "0x" + raw.hex(), "0x" + digest.hex()


def _reconcile(receipt: Any, *, side: str, wallet: str, token: str, value_wei: int,
               pre_balance: int, post_balance: int, min_out: int) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    try:
        status = int(str(receipt.get("status") or "0x0"), 16)
        gas_used = int(str(receipt.get("gasUsed") or "0x0"), 16)
        gas_price = int(str(receipt.get("effectiveGasPrice") or "0x0"), 16)
        block = int(str(receipt.get("blockNumber") or "0x0"), 16)
    except (TypeError, ValueError):
        return None
    if status != 1:
        raise RuntimeError("broadcast transaction failed on chain")
    fee = gas_used * gas_price
    token_delta = _token_delta(receipt.get("logs"), token=token, wallet=wallet)
    if side == "buy":
        if token_delta < min_out:
            return None
        native_delta = -(value_wei + fee)
        amount_in, amount_out = value_wei, token_delta
    else:
        received = (post_balance - pre_balance) + fee
        if token_delta >= 0 or received < min_out:
            return None
        native_delta = post_balance - pre_balance
        amount_in, amount_out = abs(token_delta), received
    return {
        "status": "confirmed",
        "amount_in": str(amount_in),
        "amount_out": str(amount_out),
        "fee_wei": str(fee),
        "native_delta_wei": str(native_delta),
        "token_delta_raw": str(token_delta),
        "block_number": block,
    }


def _token_delta(logs: Any, *, token: str, wallet: str) -> int:
    if not isinstance(logs, list):
        return 0
    wallet_word = "0x" + _address_word(wallet)
    received = spent = 0
    for log in logs:
        if not isinstance(log, dict) or str(log.get("address", "")).lower() != token.lower():
            continue
        topics = log.get("topics") if isinstance(log.get("topics"), list) else []
        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        try:
            amount = int(str(log.get("data") or "0x0"), 16)
        except (TypeError, ValueError):
            continue
        if str(topics[2]).lower() == wallet_word:
            received += amount
        if str(topics[1]).lower() == wallet_word:
            spent += amount
    return received - spent


def _confirm(client: httpx.Client, rpc_url: str, tx_hash: str) -> Any:
    attempts = _env_int("NOEMA_EVM_CONFIRM_ATTEMPTS", 30, minimum=1, maximum=120)
    interval = _env_int("NOEMA_EVM_CONFIRM_INTERVAL_SECONDS", 1, minimum=0, maximum=30)
    for attempt in range(attempts):
        receipt = _rpc(client, rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            return receipt
        if interval and attempt + 1 < attempts:
            time.sleep(interval)
    raise RuntimeError("broadcast transaction confirmation timed out")


def _assert_chain_id(client: httpx.Client, rpc_url: str, expected: int) -> None:
    try:
        chain_id = int(str(_rpc(client, rpc_url, "eth_chainId", [])), 16)
    except (TypeError, ValueError):
        raise RuntimeError("EVM RPC response was incomplete") from None
    if chain_id != expected:
        raise RuntimeError("EVM RPC chain ID does not match configured chain")


def _token_metadata(client: httpx.Client, rpc_url: str, token: str) -> int:
    code = _rpc(client, rpc_url, "eth_getCode", [token, "latest"])
    if not isinstance(code, str) or code in {"0x", "0x0"}:
        raise RuntimeError("token contract is invalid")
    try:
        raw = _rpc(client, rpc_url, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        decimals = int(str(raw), 16)
    except (RuntimeError, TypeError, ValueError):
        raise RuntimeError("token contract is invalid") from None
    if not 0 <= decimals <= 18:
        raise RuntimeError("token contract is invalid")
    return decimals


def _require_allowance(client: httpx.Client, rpc_url: str, token: str, owner: str, amount: int) -> None:
    data = "0xdd62ed3e" + _address_word(owner) + _address_word(KYBER_ROUTER)
    try:
        raw = _rpc(client, rpc_url, "eth_call", [{"to": token, "data": data}, "latest"])
        allowance = int(str(raw), 16)
    except (RuntimeError, TypeError, ValueError):
        raise RuntimeError("token allowance is insufficient") from None
    if allowance < amount:
        raise RuntimeError("token allowance is insufficient")


def _require_token_balance(client: httpx.Client, rpc_url: str, token: str, owner: str, amount: int) -> None:
    data = "0x70a08231" + _address_word(owner)
    try:
        raw = _rpc(client, rpc_url, "eth_call", [{"to": token, "data": data}, "latest"])
        balance = int(str(raw), 16)
    except (RuntimeError, TypeError, ValueError):
        raise RuntimeError("token contract is invalid") from None
    if balance < amount:
        raise RuntimeError("swap request is invalid")


def _balance(client: httpx.Client, rpc_url: str, address: str) -> int:
    return _quantity(client, rpc_url, "eth_getBalance", [address, "latest"])


def _gas_price(client: httpx.Client, rpc_url: str) -> int:
    try:
        price = int(str(_rpc(client, rpc_url, "eth_gasPrice", [])), 16)
    except (TypeError, ValueError):
        raise RuntimeError("EVM RPC response was incomplete") from None
    ceiling = _env_int("NOEMA_EVM_MAX_GAS_PRICE_WEI", 500_000_000_000, minimum=1, maximum=10**18)
    if price <= 0 or price > ceiling:
        raise RuntimeError("evm fee exceeds signer maximum")
    return price


def _fee_within_cap(fee_wei: int) -> None:
    ceiling = _env_int("NOEMA_EVM_MAX_FEE_WEI", 20_000_000_000_000_000, minimum=1, maximum=10**24)
    if fee_wei <= 0 or fee_wei > ceiling:
        raise RuntimeError("evm fee exceeds signer maximum")


def _quantity(client: httpx.Client, rpc_url: str, method: str, params: list[Any]) -> int:
    try:
        return int(str(_rpc(client, rpc_url, method, params)), 16)
    except (TypeError, ValueError):
        raise RuntimeError("EVM RPC response was incomplete") from None


def _rpc(client: httpx.Client, endpoint: str, method: str, params: list[Any]) -> Any:
    try:
        response = client.post(
            endpoint, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        raise RuntimeError("EVM RPC request failed") from None
    if not isinstance(body, dict) or body.get("error") or "result" not in body:
        raise RuntimeError("EVM RPC request failed")
    return body["result"]


def _aggregator_headers() -> dict[str, str]:
    client_id = os.getenv("NOEMA_KYBERSWAP_CLIENT_ID", "").strip() or "noema"
    if len(client_id) > 64 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
                                  for char in client_id):
        client_id = "noema"
    headers = {"x-client-id": client_id}
    key = os.getenv("NOEMA_KYBERSWAP_API_KEY", "").strip()
    if key:
        headers["X-Api-Key"] = key
    return headers


def _authority_path() -> Path | None:
    override = os.getenv("NOEMA_AUTONOMY_AUTHORITY_PATH", "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise RuntimeError("swap caps are not configured")
        return path
    for path in (
        Path(__file__).resolve().parents[1] / "config" / "autonomy_authority.toml",
        Path.cwd() / "config" / "autonomy_authority.toml",
    ):
        if path.is_file():
            return path
    return None


def _native_price(chain_name: str) -> Decimal:
    raw = os.getenv("NOEMA_EVM_NATIVE_PRICES_USD", "").strip()
    if not raw:
        raise RuntimeError("evm native price is not configured")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError
        value = Decimal(str(payload[chain_name]))
    except (json.JSONDecodeError, KeyError, TypeError, InvalidOperation):
        raise RuntimeError("evm native price is not configured") from None
    if not value.is_finite() or value <= 0:
        raise RuntimeError("evm native price is not configured")
    return value


def _token_set(name: str) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    for item in os.getenv(name, "").split(","):
        parsed = _token_item(item.strip())
        if parsed:
            values.add(parsed)
    return values


def _tokens_from_file(path: Path) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        parsed = _token_item(line.split("#", 1)[0].strip())
        if parsed:
            values.add(parsed)
    return values


def _token_item(item: str) -> tuple[str, str] | None:
    if not item:
        return None
    if item.count(":") != 1:
        raise RuntimeError("signer limit configuration is invalid")
    chain_name, address = item.split(":", 1)
    chain_name = chain_name.strip().lower()
    if chain_name not in SWAP_CHAINS or not _address_ok(address.strip()):
        raise RuntimeError("signer limit configuration is invalid")
    return chain_name, _checksum(address.strip()).lower()


def _rpc_url(chain: EvmSwapChain) -> str:
    for name in (f"NOEMA_EVM_RPC_URL_{chain.chain_id}", f"NOEMA_EVM_RPC_URL_{chain.name.upper()}"):
        raw = os.getenv(name, "").strip()
        if raw:
            return _endpoint(raw, None, missing="EVM RPC endpoint is not configured")
    raise RuntimeError("EVM RPC endpoint is not configured")


def _endpoint(raw: str, default: str | None, *, missing: str) -> str:
    value = raw.strip() or (default or "")
    if not value:
        raise RuntimeError(missing)
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    local = host in {"127.0.0.1", "localhost"}
    if parsed.scheme == "https" and host:
        return value.rstrip("/")
    if parsed.scheme == "http" and local:
        return value.rstrip("/")
    raise RuntimeError(missing)


def _env_decimal(name: str) -> Decimal | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise RuntimeError("signer limit configuration is invalid") from None
    if not value.is_finite() or value <= 0:
        raise RuntimeError("signer limit configuration is invalid")
    return value


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw, 10)
        except ValueError:
            raise RuntimeError("signer limit configuration is invalid") from None
    if isinstance(value, bool) or value < minimum or value > maximum:
        raise RuntimeError("signer limit configuration is invalid")
    return value


def _usd(wei: int, price: Decimal) -> Decimal:
    return (Decimal(wei) / _WEI) * price


def _money(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _address_ok(value: str) -> bool:
    return bool(isinstance(value, str) and len(value) == 42 and value.startswith("0x")
                and all(char in "0123456789abcdefABCDEF" for char in value[2:]))


def _checksum(address: str) -> str:
    from eth_utils import to_checksum_address

    try:
        return to_checksum_address(address)
    except (ValueError, TypeError):
        raise RuntimeError("token contract is invalid") from None


def _address_word(address: str) -> str:
    return "000000000000000000000000" + address[2:].lower()


def _request_hash(fields: dict[str, Any]) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _state_path() -> Path:
    raw = os.getenv("NOEMA_EVM_SIGNER_STATE_PATH", "").strip() or "data/evm-signer-state.db"
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_state_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS evm_swap_requests (
            idempotency_key TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            tx_hash TEXT,
            raw_tx TEXT,
            notional_usd TEXT,
            day TEXT,
            pre_balance_wei TEXT,
            value_wei TEXT,
            token_decimals INTEGER,
            error TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS evm_swap_daily (
            day TEXT PRIMARY KEY,
            usd TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _daily(conn: sqlite3.Connection, day: str) -> Decimal:
    row = conn.execute("SELECT usd FROM evm_swap_daily WHERE day=?", (day,)).fetchone()
    return Decimal(row["usd"]) if row else Decimal(0)


def _add_daily(conn: sqlite3.Connection, day: str, delta: Decimal) -> None:
    updated = _daily(conn, day) + delta
    if updated < 0:
        updated = Decimal(0)
    rendered = _money(updated)
    existing = conn.execute("SELECT 1 FROM evm_swap_daily WHERE day=?", (day,)).fetchone()
    if existing:
        conn.execute("UPDATE evm_swap_daily SET usd=? WHERE day=?", (rendered, day))
    else:
        conn.execute("INSERT INTO evm_swap_daily (day, usd) VALUES (?, ?)", (day, rendered))


def _enforce_cap(notional: Decimal, per_trade: Decimal, daily_cap: Decimal, used: Decimal) -> None:
    if notional > per_trade:
        raise RuntimeError("per-trade cap exceeded")
    if used + notional > daily_cap:
        raise RuntimeError("daily cap exceeded")


class _Resume:
    def __init__(self, tx_hash: str, raw_tx: str, notional_usd: str, day: str,
                 pre_balance_wei: str, value_wei: str, token_decimals: int) -> None:
        self.tx_hash = tx_hash
        self.raw_tx = raw_tx
        self.notional_usd = notional_usd
        self.day = day
        self.pre_balance_wei = pre_balance_wei
        self.value_wei = value_wei
        self.token_decimals = token_decimals


def _load_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM evm_swap_requests WHERE idempotency_key=?", (key,),
    ).fetchone()


def _remember_failure(conn: sqlite3.Connection, key: str, message: str, *,
                      release: Decimal | None = None, day: str | None = None) -> None:
    conn.execute("BEGIN IMMEDIATE")
    row = _load_row(conn, key)
    if row and row["status"] == "confirmed":
        conn.commit()
        return
    if release is not None and day and row and row["notional_usd"] and row["status"] != "failed":
        _add_daily(conn, day, -release)
    conn.execute(
        """UPDATE evm_swap_requests SET status='failed', error=?, raw_tx=NULL, updated_at=?
           WHERE idempotency_key=?""",
        (message, _now(), key),
    )
    conn.commit()


def _claim(conn: sqlite3.Connection, key: str, digest: str) -> dict[str, Any] | _Resume | None:
    conn.execute("BEGIN IMMEDIATE")
    row = _load_row(conn, key)
    if row is None:
        stamp = _now()
        conn.execute(
            """INSERT INTO evm_swap_requests
               (idempotency_key, request_hash, status, created_at, updated_at)
               VALUES (?, ?, 'signing', ?, ?)""",
            (key, digest, stamp, stamp),
        )
        conn.commit()
        return None
    if row["request_hash"] != digest:
        conn.commit()
        raise RuntimeError("idempotency key is invalid")
    if row["status"] == "confirmed" and row["result_json"]:
        conn.commit()
        return public_swap_result(json.loads(row["result_json"]))
    if row["status"] == "failed":
        conn.commit()
        raise RuntimeError(row["error"] or "broadcast transaction failed on chain")
    if (row["raw_tx"] and row["tx_hash"] and row["notional_usd"] and row["day"]
            and row["pre_balance_wei"] and row["value_wei"] is not None and row["token_decimals"] is not None):
        resumed = _Resume(
            row["tx_hash"], row["raw_tx"], row["notional_usd"], row["day"],
            row["pre_balance_wei"], row["value_wei"], int(row["token_decimals"]),
        )
        conn.commit()
        return resumed
    conn.commit()
    return None


def _store_signed(conn: sqlite3.Connection, key: str, raw_hex: str, tx_hash: str, notional: Decimal,
                  day: str, *, pre_balance: int, value_wei: int, decimals: int, per_trade: Decimal,
                  daily_cap: Decimal) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    row = _load_row(conn, key)
    if row is None:
        conn.commit()
        raise RuntimeError("prior swap outcome is unknown; not retrying with a new transaction")
    if row["status"] == "confirmed" and row["result_json"]:
        conn.commit()
        return public_swap_result(json.loads(row["result_json"]))
    if row["raw_tx"] and row["tx_hash"]:
        payload = {
            "status": "signed", "tx_hash": row["tx_hash"], "raw_tx": row["raw_tx"],
            "notional_usd": row["notional_usd"], "day": row["day"],
            "pre_balance_wei": row["pre_balance_wei"], "value_wei": row["value_wei"],
            "token_decimals": row["token_decimals"],
        }
        conn.commit()
        return payload
    _enforce_cap(notional, per_trade, daily_cap, _daily(conn, day))
    _add_daily(conn, day, notional)
    conn.execute(
        """UPDATE evm_swap_requests
           SET status='submitted', tx_hash=?, raw_tx=?, notional_usd=?, day=?,
               pre_balance_wei=?, value_wei=?, token_decimals=?, updated_at=?
           WHERE idempotency_key=?""",
        (tx_hash, raw_hex, _money(notional), day, str(pre_balance), str(value_wei), decimals, _now(), key),
    )
    conn.commit()
    return {
        "status": "signed", "tx_hash": tx_hash, "raw_tx": raw_hex,
        "notional_usd": _money(notional), "day": day,
        "pre_balance_wei": str(pre_balance), "value_wei": str(value_wei),
        "token_decimals": decimals,
    }
