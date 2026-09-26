from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from re import fullmatch

import httpx


@dataclass(frozen=True)
class EvmWalletSnapshot:
    address: str
    chain_id: int
    block_number: int
    nonce: int
    native_balance_wei: int
    native_balance: Decimal


class EvmWatchClient:
    """Read-only EVM JSON-RPC watcher for NOEMA's dedicated agent wallet."""

    def __init__(
        self,
        *,
        rpc_url: str,
        address: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not rpc_url:
            raise ValueError("rpc_url is required")
        if fullmatch(r"0x[a-fA-F0-9]{40}", address) is None:
            raise ValueError("EVM address must be a 20-byte 0x-prefixed hex address")
        self.address = address
        self.client = client or httpx.AsyncClient(
            base_url=rpc_url,
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": "NOEMA/0.1"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _rpc(self, method: str, params: list[object]) -> object:
        response = await self.client.post(
            "",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"EVM RPC error: {payload['error']}")
        return payload["result"]

    async def snapshot(self) -> EvmWalletSnapshot:
        chain_hex = str(await self._rpc("eth_chainId", []))
        block_hex = str(await self._rpc("eth_blockNumber", []))
        nonce_hex = str(
            await self._rpc(
                "eth_getTransactionCount",
                [self.address, "latest"],
            )
        )
        balance_hex = str(
            await self._rpc(
                "eth_getBalance",
                [self.address, "latest"],
            )
        )

        balance_wei = int(balance_hex, 16)
        return EvmWalletSnapshot(
            address=self.address,
            chain_id=int(chain_hex, 16),
            block_number=int(block_hex, 16),
            nonce=int(nonce_hex, 16),
            native_balance_wei=balance_wei,
            native_balance=Decimal(balance_wei) / Decimal(10**18),
        )
