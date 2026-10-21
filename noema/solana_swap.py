"""Jupiter quote, guarded swap build, sign, send, and confirm.

The private key never enters this module. The signer process loads it and passes
only the keypair object into ``perform_jupiter_swap``. Caps, the RPC URL, and the
Jupiter URL come from the signer environment, never from the IPC request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from .solana_live_gate import LIVE_VENUE_NAMES, solana_venue_selected

WSOL_MINT = "So11111111111111111111111111111111111111112"
# The only address this swap signer will sign for. A matching env value is
# required as well; a different derived key is refused.
EXPECTED_SOLANA_SIGNER_ADDRESS = "J57oJ1GAraQuts7htkrRN7mzhkq93sqM3iRKMRuHn5sv"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ALLOWED_EXTENSIONS = frozenset({"metadatapointer", "tokenmetadata"})
DEFAULT_JUPITER_URL = "https://api.jup.ag/swap/v1"
REQUEST_FIELDS = ("side", "mint", "amount", "max_slippage_bps", "min_out", "idempotency_key")
PUBLIC_RESULT_FIELDS = (
    "status", "signature", "address", "mint", "side", "amount_in", "amount_out",
    "min_out", "fee_lamports", "sol_delta_lamports", "token_delta_raw", "token_decimals",
    "slot", "idempotency_key", "notional_usd", "sol_price_usd", "price_timestamp",
)
_LAMPORTS = Decimal(1_000_000_000)


def public_swap_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in PUBLIC_RESULT_FIELDS if key in payload}


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
        raise RuntimeError("priority fee exceeds signer maximum" if "PRIORITY" in name
                           else "signer limit configuration is invalid")
    return value


def authority_config_path() -> Path | None:
    override = os.getenv("NOEMA_AUTONOMY_AUTHORITY_PATH", "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise RuntimeError("swap caps are not configured")
        return path
    candidates = (
        Path(__file__).resolve().parents[1] / "config" / "autonomy_authority.toml",
        Path.cwd() / "config" / "autonomy_authority.toml",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def resolve_usd_caps() -> tuple[Decimal, Decimal]:
    """Per-trade and daily USD caps. Fractions win when the authority file exists.

    An explicit ``NOEMA_SOLANA_MAX_*`` value can only tighten those fractions.
    When the file is absent, both env ceilings are required.
    """
    path = authority_config_path()
    env_trade = _env_decimal("NOEMA_SOLANA_MAX_TRADE_USD")
    env_daily = _env_decimal("NOEMA_SOLANA_MAX_DAILY_USD")
    if path is None:
        if env_trade is None or env_daily is None:
            raise RuntimeError("swap caps are not configured")
        return env_trade, env_daily
    import tomllib

    level = os.getenv("NOEMA_AUTONOMY_LEVEL", "").strip().lower()
    bankroll = _env_decimal("NOEMA_SOLANA_BANKROLL_USD")
    if not level or bankroll is None:
        raise RuntimeError("swap caps are not configured")
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


def _endpoint(name: str, default: str | None, *, missing: str) -> str:
    raw = os.getenv(name, "").strip() or (default or "")
    if not raw:
        raise RuntimeError(missing)
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    local = host in {"127.0.0.1", "localhost"}
    if parsed.scheme == "https" and host:
        return raw.rstrip("/")
    if parsed.scheme == "http" and local:
        return raw.rstrip("/")
    raise RuntimeError(missing)


def _rpc(client: httpx.Client, endpoint: str, method: str, params: list[Any]) -> Any:
    try:
        response = client.post(
            endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        raise RuntimeError("Solana RPC request failed") from None
    if not isinstance(body, dict) or body.get("error"):
        raise RuntimeError("Solana RPC request failed")
    if "result" not in body:
        raise RuntimeError("Solana RPC response was incomplete")
    return body["result"]


def _jupiter_headers() -> dict[str, str]:
    key = os.getenv("NOEMA_JUPITER_API_KEY", "").strip()
    return {"x-api-key": key} if key else {}


def _denylist() -> set[str]:
    values = {
        item.strip()
        for item in os.getenv("NOEMA_SOLANA_MINT_DENYLIST", "").split(",")
        if item.strip()
    }
    path_raw = os.getenv("NOEMA_SOLANA_MINT_DENYLIST_PATH", "").strip()
    path = Path(path_raw) if path_raw else (
        Path(__file__).resolve().parents[1] / "config" / "solana_mint_denylist.txt"
    )
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            item = line.split("#", 1)[0].strip()
            if item:
                values.add(item)
    return values


def _extension_name(item: Any) -> str:
    if isinstance(item, str):
        return item.lower()
    if isinstance(item, dict):
        raw = item.get("extension") or item.get("type") or ""
        return str(raw).lower()
    return "unknown"


def inspect_mint_account(account: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(account, dict):
        raise RuntimeError("token program is not an allowed SPL token")  # noqa: TRY004
    owner = str(account.get("owner") or "")
    if owner not in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
        raise RuntimeError("token program is not an allowed SPL token")
    parsed = ((account.get("data") or {}).get("parsed") or {})
    if parsed.get("type") != "mint":
        raise RuntimeError("token program is not an allowed SPL token")
    info = parsed.get("info") or {}
    if info.get("freezeAuthority") is not None:
        raise RuntimeError("mint retains freeze authority")
    if info.get("mintAuthority") is not None:
        raise RuntimeError("mint retains mint authority")
    try:
        decimals = int(info["decimals"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("token program is not an allowed SPL token") from None
    if not 0 <= decimals <= 18:
        raise RuntimeError("token program is not an allowed SPL token")
    for extension in info.get("extensions") or []:
        name = _extension_name(extension)
        if name not in ALLOWED_EXTENSIONS:
            raise RuntimeError("token extension is not allowed")
    return {"decimals": decimals, "owner": owner}


def _request_hash(fields: dict[str, Any]) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _parse_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RuntimeError("swap request is invalid")  # noqa: TRY004
    side = request.get("side")
    mint = request.get("mint")
    amount = request.get("amount")
    slippage = request.get("max_slippage_bps")
    minimum = request.get("min_out")
    key = request.get("idempotency_key")
    if side not in {"buy", "sell"}:
        raise RuntimeError("swap side is not allowed")
    if not isinstance(mint, str):
        raise RuntimeError("mint is not a valid token address")  # noqa: TRY004
    try:
        from solders.pubkey import Pubkey

        mint_key = str(Pubkey.from_string(mint))
    except (ValueError, TypeError):
        raise RuntimeError("mint is not a valid token address") from None
    if mint_key in {WSOL_MINT}:
        raise RuntimeError("mint is not a valid token address")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount < 2**64:
        raise RuntimeError("swap request is invalid")
    if isinstance(slippage, bool) or not isinstance(slippage, int):
        raise RuntimeError("slippage exceeds signer maximum")  # noqa: TRY004
    configured = _env_int("NOEMA_SOLANA_MAX_SLIPPAGE_BPS", 100, minimum=1, maximum=2_000)
    if slippage < 1 or slippage > configured:
        raise RuntimeError("slippage exceeds signer maximum")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
        raise RuntimeError("minimum output is invalid")
    if not isinstance(key, str) or not 4 <= len(key) <= 200:
        raise RuntimeError("idempotency key is invalid")
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:._-+" for char in key):
        raise RuntimeError("idempotency key is invalid")
    if mint_key in _denylist():
        raise RuntimeError("mint is denylisted")
    return {
        "side": side, "mint": mint_key, "amount": amount,
        "max_slippage_bps": slippage, "min_out": minimum, "idempotency_key": key,
    }


def _require_gates() -> None:
    if os.getenv("NOEMA_SOLANA_SIGNER_ENABLED", "0") != "1" or os.getenv("NOEMA_ALLOW_LIVE_ORDERS", "0") != "1":
        raise RuntimeError("solana live swaps are disabled")
    if os.getenv("NOEMA_SOLANA_OWNER_REVIEWED", "0") != "1":
        raise RuntimeError("solana owner review is required")
    venues = {
        item.strip() for item in os.getenv("NOEMA_LIVE_VENUES", "").split(",") if item.strip()
    }
    if not (venues & LIVE_VENUE_NAMES) or not solana_venue_selected():
        raise RuntimeError("solana venue is not allowlisted")
    if os.getenv("NOEMA_MASTER_HALT", "0") == "1":
        raise RuntimeError("owner master halt is active")
    if os.getenv("NOEMA_SOLANA_MASTER_HALT", "1") != "0":
        raise RuntimeError("solana master halt is active")
    if os.getenv("NOEMA_WALLET_MASTER_HALT", "1") != "0":
        raise RuntimeError("wallet master halt is active")


def _state_path() -> Path:
    raw = os.getenv("NOEMA_SOLANA_SIGNER_STATE_PATH", "").strip() or "data/solana-signer-state.db"
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_state_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS solana_swap_requests (
            idempotency_key TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            signature TEXT,
            signed_tx TEXT,
            notional_usd TEXT,
            day TEXT,
            error TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS solana_swap_daily (
            day TEXT PRIMARY KEY,
            usd TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _daily(conn: sqlite3.Connection, day: str) -> Decimal:
    row = conn.execute("SELECT usd FROM solana_swap_daily WHERE day=?", (day,)).fetchone()
    return Decimal(row["usd"]) if row else Decimal(0)


def _add_daily(conn: sqlite3.Connection, day: str, delta: Decimal) -> None:
    updated = _daily(conn, day) + delta
    if updated < 0:
        updated = Decimal(0)
    rendered = format(updated, "f")
    existing = conn.execute("SELECT 1 FROM solana_swap_daily WHERE day=?", (day,)).fetchone()
    if existing:
        conn.execute("UPDATE solana_swap_daily SET usd=? WHERE day=?", (rendered, day))
    else:
        conn.execute("INSERT INTO solana_swap_daily (day, usd) VALUES (?, ?)", (day, rendered))


def _usd(lamports: int, price: Decimal) -> Decimal:
    return (Decimal(lamports) / _LAMPORTS) * price


def _money(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _enforce_cap(notional: Decimal, per_trade: Decimal, daily_cap: Decimal, used: Decimal) -> None:
    if notional > per_trade:
        raise RuntimeError("per-trade cap exceeded")
    if used + notional > daily_cap:
        raise RuntimeError("daily cap exceeded")


def _balance_covers(lamports: int, *, spend: int, reserve: int, priority_fee: int) -> None:
    cushion = priority_fee + 10_000
    if lamports < spend + cushion + reserve:
        raise RuntimeError("solana reserve would be breached")


def _quote(client: httpx.Client, base: str, fields: dict[str, Any]) -> dict[str, Any]:
    sell = WSOL_MINT if fields["side"] == "buy" else fields["mint"]
    buy = fields["mint"] if fields["side"] == "buy" else WSOL_MINT
    try:
        response = client.get(
            f"{base}/quote",
            params={
                "inputMint": sell,
                "outputMint": buy,
                "amount": str(fields["amount"]),
                "slippageBps": str(fields["max_slippage_bps"]),
                "swapMode": "ExactIn",
            },
            headers=_jupiter_headers(),
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        raise RuntimeError("jupiter quote failed") from None
    if not isinstance(body, dict):
        raise RuntimeError("jupiter quote failed")  # noqa: TRY004
    try:
        in_amount = int(body["inAmount"])
        out_amount = int(body["outAmount"])
        threshold = int(body["otherAmountThreshold"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("jupiter quote failed") from None
    if body.get("inputMint") != sell or body.get("outputMint") != buy or in_amount != fields["amount"]:
        raise RuntimeError("quote does not match the requested swap")
    if out_amount <= 0 or threshold < fields["min_out"] or out_amount < fields["min_out"]:
        raise RuntimeError("quote is below minimum output")
    return body


def _swap_transaction(client: httpx.Client, base: str, quote: dict[str, Any],
                      address: str, priority_fee: int) -> bytes:
    try:
        response = client.post(
            f"{base}/swap",
            json={
                "quoteResponse": quote,
                "userPublicKey": address,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": priority_fee,
            },
            headers=_jupiter_headers(),
        )
        response.raise_for_status()
        body = response.json()
        encoded = body["swapTransaction"]
        raw = base64.b64decode(encoded, validate=True)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise RuntimeError("jupiter swap build failed") from None
    if not raw:
        raise RuntimeError("jupiter swap build failed")
    return raw


def _sign(raw: bytes, keypair: Any, address: str) -> tuple[bytes, str]:
    from solders.transaction import SanitizeError, TransactionError, VersionedTransaction

    try:
        transaction = VersionedTransaction.from_bytes(raw)
        message = transaction.message
        if str(message.account_keys[0]) != address:
            raise RuntimeError("swap transaction fee payer is not the signer")
        if int(message.header.num_required_signatures) != 1:
            raise RuntimeError("swap transaction requires an unexpected signer")
        signed = VersionedTransaction(message, [keypair])
    except RuntimeError:
        raise
    except (SanitizeError, TransactionError, ValueError, TypeError, AttributeError, IndexError):
        raise RuntimeError("jupiter swap build failed") from None
    encoded = bytes(signed)
    signature = str(signed.signatures[0])
    if not signature or signature.startswith("1" * 32):
        raise RuntimeError("jupiter swap build failed")
    return encoded, signature


def _simulate(client: httpx.Client, rpc_url: str, encoded: bytes) -> None:
    payload = base64.b64encode(encoded).decode("ascii")
    simulation = _rpc(
        client, rpc_url, "simulateTransaction",
        [payload, {
            "encoding": "base64", "commitment": "confirmed",
            "sigVerify": False, "replaceRecentBlockhash": True,
        }],
    )
    value = simulation.get("value") if isinstance(simulation, dict) else None
    if not isinstance(value, dict) or value.get("err") is not None:
        raise RuntimeError("transaction simulation rejected")


def _send(client: httpx.Client, rpc_url: str, encoded: bytes, expected: str) -> str:
    payload = base64.b64encode(encoded).decode("ascii")
    signature = _rpc(
        client, rpc_url, "sendTransaction",
        [payload, {
            "encoding": "base64", "skipPreflight": False,
            "preflightCommitment": "confirmed", "maxRetries": 3,
        }],
    )
    if not isinstance(signature, str) or not signature:
        raise RuntimeError("broadcast transaction returned no signature")
    if signature != expected:
        raise RuntimeError("broadcast transaction identity mismatch")
    return signature


def _confirm(client: httpx.Client, rpc_url: str, signature: str) -> dict[str, Any]:
    attempts = _env_int("NOEMA_SOLANA_CONFIRM_ATTEMPTS", 30, minimum=1, maximum=60)
    interval = os.getenv("NOEMA_SOLANA_CONFIRM_INTERVAL_SECONDS", "1").strip() or "1"
    try:
        pause = float(interval)
    except ValueError:
        pause = 1.0
    pause = min(5.0, max(0.0, pause))
    for _ in range(attempts):
        statuses = _rpc(
            client, rpc_url, "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": True}],
        )
        values = statuses.get("value") if isinstance(statuses, dict) else None
        status = values[0] if isinstance(values, list) and values else None
        if isinstance(status, dict) and status.get("err"):
            raise RuntimeError("broadcast transaction failed on chain")
        if isinstance(status, dict) and status.get("confirmationStatus") in {"confirmed", "finalized"}:
            transaction = _rpc(
                client, rpc_url, "getTransaction",
                [signature, {
                    "encoding": "jsonParsed", "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                }],
            )
            if not isinstance(transaction, dict):
                raise RuntimeError("Solana RPC response was incomplete")
            return transaction
        if pause:
            time.sleep(pause)
    raise RuntimeError("broadcast transaction confirmation timed out")


def _account_key(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("pubkey") or "")
    return ""


def _token_raw(rows: Any, *, wallet: str, mint: str) -> tuple[int, int | None]:
    total = 0
    decimals: int | None = None
    if not isinstance(rows, list):
        return total, decimals
    for row in rows:
        if not isinstance(row, dict) or row.get("mint") != mint or row.get("owner") != wallet:
            continue
        token = row.get("uiTokenAmount") or {}
        try:
            total += int(token["amount"])
            decimals = int(token["decimals"])
        except (KeyError, TypeError, ValueError):
            continue
    return total, decimals


def parse_fill(transaction: dict[str, Any], *, wallet: str, mint: str, side: str) -> dict[str, Any]:
    meta = transaction.get("meta") if isinstance(transaction.get("meta"), dict) else None
    if meta is None or meta.get("err"):
        raise RuntimeError("broadcast transaction failed on chain")
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []
    try:
        index = next(i for i, item in enumerate(keys) if _account_key(item) == wallet)
        pre = int(meta["preBalances"][index])
        post = int(meta["postBalances"][index])
        fee = int(meta["fee"])
    except (StopIteration, KeyError, TypeError, ValueError, IndexError):
        raise RuntimeError("swap fill could not be reconciled") from None
    pre_token, pre_decimals = _token_raw(meta.get("preTokenBalances"), wallet=wallet, mint=mint)
    post_token, post_decimals = _token_raw(meta.get("postTokenBalances"), wallet=wallet, mint=mint)
    decimals = post_decimals if post_decimals is not None else pre_decimals
    if decimals is None:
        raise RuntimeError("swap fill could not be reconciled")
    sol_delta = post - pre
    token_delta = post_token - pre_token
    reconciled = (
        (side == "buy" and token_delta > 0 and sol_delta < 0)
        or (side == "sell" and token_delta < 0 and sol_delta + fee > 0)
    )
    return {
        "fee_lamports": fee,
        "sol_delta_lamports": sol_delta,
        "token_delta_raw": str(token_delta),
        "token_decimals": decimals,
        "amount_in": str(abs(sol_delta) if side == "buy" else abs(token_delta)),
        "amount_out": str(token_delta if side == "buy" else sol_delta + fee),
        "status": "confirmed" if reconciled else "unreconciled",
        "slot": transaction.get("slot"),
    }


class _Resume:
    def __init__(self, signature: str, signed_tx: str, notional_usd: str, day: str) -> None:
        self.signature = signature
        self.signed_tx = signed_tx
        self.notional_usd = notional_usd
        self.day = day


def _load_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM solana_swap_requests WHERE idempotency_key=?", (key,),
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
        """UPDATE solana_swap_requests SET status='failed', error=?, signed_tx=NULL, updated_at=?
           WHERE idempotency_key=?""",
        (message, _now(), key),
    )
    conn.commit()


def perform_jupiter_swap(request: dict[str, Any], keypair: Any, *,
                         client: httpx.Client | None = None) -> dict[str, Any]:
    """Quote, sign, send, and confirm one allowlisted SOL<->SPL swap."""
    fields = _parse_request(request)
    _require_gates()
    address = str(keypair.pubkey())
    configured = os.getenv("NOEMA_SOLANA_WALLET_ADDRESS", "").strip()
    if (not configured or address != EXPECTED_SOLANA_SIGNER_ADDRESS
            or configured != EXPECTED_SOLANA_SIGNER_ADDRESS):
        raise RuntimeError("configured solana wallet does not match signer keypair")
    per_trade, daily_cap = resolve_usd_caps()
    price = _env_decimal("NOEMA_SOLANA_PRICE_USD")
    if price is None:
        raise RuntimeError("solana price is not configured")
    priority_fee = _env_int(
        "NOEMA_SOLANA_PRIORITY_FEE_LAMPORTS", 100_000, minimum=0, maximum=1_000_000_000,
    )
    reserve = _env_int(
        "NOEMA_SOLANA_FEE_RESERVE_LAMPORTS", 20_000_000, minimum=5_000, maximum=10**12,
    )
    rpc_url = _endpoint("NOEMA_SOLANA_RPC_URL", None, missing="Solana RPC endpoint is not configured")
    jupiter = _endpoint(
        "NOEMA_JUPITER_SWAP_API_URL", DEFAULT_JUPITER_URL, missing="jupiter api endpoint is not configured",
    )
    owned = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        return _perform(
            fields, keypair, address, client,
            per_trade=per_trade, daily_cap=daily_cap, price=price,
            priority_fee=priority_fee, reserve=reserve, rpc_url=rpc_url, jupiter=jupiter,
        )
    finally:
        if owned:
            client.close()


def _perform(fields: dict[str, Any], keypair: Any, address: str, client: httpx.Client, *,
             per_trade: Decimal, daily_cap: Decimal, price: Decimal, priority_fee: int,
             reserve: int, rpc_url: str, jupiter: str) -> dict[str, Any]:
    digest = _request_hash({key: fields[key] for key in REQUEST_FIELDS})
    conn = _connect()
    try:
        resumed = _claim(conn, fields["idempotency_key"], digest)
        if isinstance(resumed, dict):
            return resumed
        if isinstance(resumed, _Resume):
            signature = resumed.signature
            encoded = base64.b64decode(resumed.signed_tx)
            notional = Decimal(resumed.notional_usd)
            day = resumed.day
        else:
            account = _rpc(
                client, rpc_url, "getAccountInfo",
                [fields["mint"], {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
            inspect_mint_account((account or {}).get("value") if isinstance(account, dict) else None)
            balance = _rpc(client, rpc_url, "getBalance", [address, {"commitment": "confirmed"}])
            lamports = int(balance["value"]) if isinstance(balance, dict) else int(balance)
            spend = fields["amount"] if fields["side"] == "buy" else 0
            _balance_covers(lamports, spend=spend, reserve=reserve, priority_fee=priority_fee)
            if fields["side"] == "buy":
                _enforce_cap(_usd(fields["amount"], price), per_trade, daily_cap, Decimal(0))
            quote = _quote(client, jupiter, fields)
            if fields["side"] == "sell":
                _enforce_cap(_usd(int(quote["outAmount"]), price), per_trade, daily_cap, Decimal(0))
            notional = _usd(
                fields["amount"] if fields["side"] == "buy" else int(quote["outAmount"]), price,
            )
            raw = _swap_transaction(client, jupiter, quote, address, priority_fee)
            encoded, signature = _sign(raw, keypair, address)
            _simulate(client, rpc_url, encoded)
            day = datetime.now(UTC).date().isoformat()
            stored = _store_signed(
                conn, fields["idempotency_key"], encoded, signature, notional, day,
                per_trade=per_trade, daily_cap=daily_cap,
            )
            if stored.get("status") == "confirmed":
                return public_swap_result(stored)
            signature = str(stored["signature"])
            encoded = base64.b64decode(str(stored["signed_tx"]))
            notional = Decimal(str(stored["notional_usd"]))
            day = str(stored["day"])
        try:
            _send(client, rpc_url, encoded, signature)
            transaction = _confirm(client, rpc_url, signature)
        except RuntimeError as exc:
            message = str(exc)
            if message == "broadcast transaction failed on chain":
                _remember_failure(
                    conn, fields["idempotency_key"], message, release=notional, day=day,
                )
            raise
        fill = parse_fill(transaction, wallet=address, mint=fields["mint"], side=fields["side"])
        result = {
            **fill,
            "signature": signature,
            "address": address,
            "mint": fields["mint"],
            "side": fields["side"],
            "min_out": str(fields["min_out"]),
            "idempotency_key": fields["idempotency_key"],
            "notional_usd": _money(notional),
            "sol_price_usd": _money(price),
            "price_timestamp": _now(),
        }
        public = public_swap_result(result)
        terminal = "confirmed" if public["status"] == "confirmed" else "failed"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE solana_swap_requests
               SET status=?, result_json=?, signed_tx=NULL, error=?, updated_at=?
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


def _claim(conn: sqlite3.Connection, key: str, digest: str) -> dict[str, Any] | _Resume | None:
    conn.execute("BEGIN IMMEDIATE")
    row = _load_row(conn, key)
    if row is None:
        stamp = _now()
        conn.execute(
            """INSERT INTO solana_swap_requests
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
    if row["signed_tx"] and row["signature"] and row["notional_usd"] and row["day"]:
        resumed = _Resume(row["signature"], row["signed_tx"], row["notional_usd"], row["day"])
        conn.commit()
        return resumed
    conn.commit()
    return None


def _store_signed(conn: sqlite3.Connection, key: str, encoded: bytes, signature: str,
                  notional: Decimal, day: str, *, per_trade: Decimal,
                  daily_cap: Decimal) -> dict[str, Any]:
    conn.execute("BEGIN IMMEDIATE")
    row = _load_row(conn, key)
    if row is None:
        conn.commit()
        raise RuntimeError("prior swap outcome is unknown; not retrying with a new transaction")
    if row["status"] == "confirmed" and row["result_json"]:
        conn.commit()
        return public_swap_result(json.loads(row["result_json"]))
    if row["signed_tx"] and row["signature"]:
        payload = {
            "status": "signed", "signature": row["signature"], "signed_tx": row["signed_tx"],
            "notional_usd": row["notional_usd"], "day": row["day"],
        }
        conn.commit()
        return payload
    used = _daily(conn, day)
    _enforce_cap(notional, per_trade, daily_cap, used)
    _add_daily(conn, day, notional)
    conn.execute(
        """UPDATE solana_swap_requests
           SET status='submitted', signature=?, signed_tx=?, notional_usd=?, day=?, updated_at=?
           WHERE idempotency_key=?""",
        (signature, base64.b64encode(encoded).decode("ascii"), _money(notional), day, _now(), key),
    )
    conn.commit()
    return {
        "status": "signed", "signature": signature,
        "signed_tx": base64.b64encode(encoded).decode("ascii"),
        "notional_usd": _money(notional), "day": day,
    }
