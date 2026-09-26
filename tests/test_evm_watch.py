from decimal import Decimal

import httpx
import pytest

from noema.evm_watch import EvmWatchClient


@pytest.mark.asyncio
async def test_evm_watch_reads_basic_wallet_state() -> None:
    calls = {
        "eth_chainId": "0x1",
        "eth_blockNumber": "0x64",
        "eth_getTransactionCount": "0x2",
        "eth_getBalance": hex(10**18),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        method = __import__("json").loads(request.content)["method"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": calls[method]})

    client = httpx.AsyncClient(
        base_url="https://rpc.example",
        transport=httpx.MockTransport(handler),
    )
    watcher = EvmWatchClient(
        rpc_url="https://rpc.example",
        address="0x1111111111111111111111111111111111111111",
        client=client,
    )
    try:
        snapshot = await watcher.snapshot()
    finally:
        await watcher.close()

    assert snapshot.chain_id == 1
    assert snapshot.block_number == 100
    assert snapshot.nonce == 2
    assert snapshot.native_balance == Decimal(1)


def test_evm_watch_rejects_bad_address() -> None:
    with pytest.raises(ValueError, match="EVM address"):
        EvmWatchClient(rpc_url="https://rpc.example", address="not-an-address")
