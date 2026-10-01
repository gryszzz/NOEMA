from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from urllib.parse import urlparse

from noema.wallet_observer import PublicWalletObserver, _valid_bitcoin_address


def clear_evm_registry_env(monkeypatch):
    for key in (
        "NOEMA_EVM_RPC_URL", "NOEMA_EVM_RPC_URL_ETHEREUM", "NOEMA_EVM_RPC_URL_BASE",
        "NOEMA_EVM_RPC_URL_POLYGON", "NOEMA_EVM_RPC_URL_1", "NOEMA_EVM_RPC_URL_8453",
        "NOEMA_EVM_RPC_URL_137", "NOEMA_EVM_CHAIN_REGISTRY_JSON",
    ):
        monkeypatch.delenv(key, raising=False)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class PublicReadClient:
    async def post(self, url, *, json):
        if json["method"] == "getBalance":
            return Response({"result": {"value": 123_456_789}})
        chain_ids = {
            "ethereum-rpc.publicnode.com": 1,
            "mainnet.base.org": 8453,
            "polygon-bor-rpc.publicnode.com": 137,
        }
        chain_id = chain_ids[urlparse(url).hostname]
        if json["method"] == "eth_chainId":
            return Response({"result": hex(chain_id)})
        if json["method"] == "eth_getBlockByNumber":
            now = int(datetime.now(UTC).timestamp())
            return Response({"result": {"number": hex(100), "timestamp": hex(now)}})
        if json["method"] == "eth_getBalance":
            return Response({"result": hex(1_250_000_000_000_000_000)})
        raise AssertionError("observer issued an unexpected RPC method")

    async def get(self, _url):
        return Response({
            "chain_stats": {"funded_txo_sum": 120, "spent_txo_sum": 20},
            "mempool_stats": {"funded_txo_sum": 5, "spent_txo_sum": 0},
        })


def test_public_wallet_observer_reads_balances_without_signing_credentials(monkeypatch):
    clear_evm_registry_env(monkeypatch)
    monkeypatch.setenv("NOEMA_SOLANA_WALLET_ADDRESS", "So11111111111111111111111111111111111111112")
    monkeypatch.setenv("NOEMA_EVM_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setenv("NOEMA_BITCOIN_ADDRESS", "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh")

    async def read():
        async with PublicWalletObserver(client=PublicReadClient()) as observer:
            return await observer.read_all()

    rows = asyncio.run(read())

    assert [row["chain"] for row in rows] == [
        "solana", "ethereum", "base", "polygon", "bitcoin",
    ]
    assert all(row["status"] == "read_only_balance" for row in rows)
    assert rows[0]["sol"] == "0.123456789"
    assert rows[1]["native_balance"] == "1.25"
    assert rows[2]["native_balance"] == "1.25"
    assert rows[3]["native_balance"] == "1.25"
    assert rows[4]["btc"] == "0.00000105"
    assert all(row["signer_configured"] is False for row in rows)
    assert all(row["rpc_health"] == "healthy" for row in rows[1:4])
    assert all(row["data_freshness"] == "fresh" for row in rows[1:4])
    assert rows[1]["canonical_network_id"] == "eip155:1"
    assert rows[2]["native_symbol"] == "ETH"


def test_public_wallet_observer_preserves_unknown_when_address_or_source_is_unavailable(monkeypatch):
    clear_evm_registry_env(monkeypatch)
    monkeypatch.delenv("NOEMA_SOLANA_WALLET_ADDRESS", raising=False)
    monkeypatch.setenv("NOEMA_EVM_ADDRESS", "not-an-address")
    monkeypatch.delenv("NOEMA_BITCOIN_ADDRESS", raising=False)

    async def read():
        async with PublicWalletObserver(client=PublicReadClient()) as observer:
            return await observer.read_all()

    rows = asyncio.run(read())
    assert rows[0]["status"] == "unconfigured"
    assert rows[1]["status"] == "invalid_address"
    assert rows[2]["status"] == "invalid_address"
    assert rows[3]["status"] == "invalid_address"
    assert rows[4]["status"] == "unconfigured"
    assert "sol" not in rows[0]
    assert rows[1]["rpc_health"] == "healthy"
    assert "native_balance" not in rows[1]
    assert "btc" not in rows[4]


def test_public_wallet_observer_never_returns_secret_rpc_url_or_error_text(monkeypatch):
    secret_endpoint = "https://rpc.example/private?api-key=fixture-secret"
    monkeypatch.setenv("NOEMA_SOLANA_WALLET_ADDRESS", "So11111111111111111111111111111111111111112")
    monkeypatch.setenv("NOEMA_SOLANA_RPC_URL", secret_endpoint)
    monkeypatch.delenv("NOEMA_EVM_ADDRESS", raising=False)
    monkeypatch.delenv("NOEMA_BITCOIN_ADDRESS", raising=False)

    class FailingClient(PublicReadClient):
        async def post(self, url, *, json):
            raise RuntimeError(f"provider failed at {url} with fixture-secret")

    async def read():
        async with PublicWalletObserver(client=FailingClient()) as observer:
            return await observer.read_all()

    rows = asyncio.run(read())
    assert rows[0]["status"] == "unavailable"
    assert rows[0]["failure_type"] == "RuntimeError"
    assert secret_endpoint not in str(rows)
    assert "fixture-secret" not in str(rows)


def test_evm_observer_rejects_rpc_chain_id_mismatch():
    from noema.chain_registry import EVMChain

    class MismatchedClient(PublicReadClient):
        async def post(self, _url, *, json):
            if json["method"] == "eth_chainId":
                return Response({"result": hex(8453)})
            raise AssertionError("RPC reads must stop after chain mismatch")

    async def read():
        async with PublicWalletObserver(client=MismatchedClient()) as observer:
            return await observer.read_evm(
                EVMChain(1, "ethereum", "ETH", "https://rpc.invalid", "configured_json_rpc"),
                "0x" + "1" * 40,
            )

    row = asyncio.run(read())
    assert row["status"] == "rpc_chain_id_mismatch"
    assert row["rpc_health"] == "misconfigured"
    assert row["actual_chain_id"] == 8453
    assert "rpc.invalid" not in str(row)


def test_invalid_registry_is_reported_without_hiding_non_evm_observations(monkeypatch):
    monkeypatch.setenv("NOEMA_EVM_CHAIN_REGISTRY_JSON", "not-json")
    monkeypatch.delenv("NOEMA_SOLANA_WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("NOEMA_BITCOIN_ADDRESS", raising=False)

    async def read():
        async with PublicWalletObserver(client=PublicReadClient()) as observer:
            return await observer.read_all()

    rows = asyncio.run(read())
    assert [row["chain"] for row in rows] == ["solana", "evm_registry", "bitcoin"]
    assert rows[1]["status"] == "registry_invalid"
    assert rows[1]["execution_authority_state"] == "disabled"


def test_bitcoin_observer_accepts_negative_unconfirmed_spend_delta(monkeypatch):
    monkeypatch.setenv("NOEMA_BITCOIN_ADDRESS", "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh")

    class PendingSpendClient(PublicReadClient):
        async def get(self, _url):
            return Response({
                "chain_stats": {"funded_txo_sum": 120, "spent_txo_sum": 20},
                "mempool_stats": {"funded_txo_sum": 5, "spent_txo_sum": 10},
            })

    async def read():
        async with PublicWalletObserver(client=PendingSpendClient()) as observer:
            return await observer.read_bitcoin(
                "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            )

    row = asyncio.run(read())
    assert row["status"] == "read_only_balance"
    assert row["confirmed_sats"] == 100
    assert row["unconfirmed_sats"] == -5
    assert row["total_sats"] == 95


def test_bitcoin_observer_accepts_uppercase_bech32_but_rejects_mixed_case():
    address = "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"
    assert _valid_bitcoin_address(address.upper()) is True
    assert _valid_bitcoin_address("bc1" + address[3:].upper()) is False
