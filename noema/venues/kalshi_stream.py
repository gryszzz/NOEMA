from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import websockets

from noema.config import KalshiConfig
from noema.venues.kalshi import KalshiSigner


@dataclass(frozen=True)
class KalshiStreamMessage:
    type: str
    payload: dict[str, Any]


class KalshiStream:
    def __init__(self, config: KalshiConfig | None = None) -> None:
        self.config = config or KalshiConfig.from_env()
        if not self.config.key_id or not self.config.private_key_path:
            raise RuntimeError("Kalshi WebSocket requires API credentials")
        self.signer = KalshiSigner(self.config.key_id, self.config.private_key_path)

    @property
    def url(self) -> str:
        if self.config.environment == "production":
            return "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
        return "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

    async def messages(
        self,
        *,
        channels: list[str],
        market_tickers: list[str] | None = None,
        reconnect: bool = True,
    ) -> AsyncIterator[KalshiStreamMessage]:
        if not channels:
            raise ValueError("at least one channel is required")

        backoff = 1.0
        while True:
            try:
                headers = self.signer.headers("GET", "/trade-api/ws/v2")
                async with websockets.connect(
                    self.url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_queue=2048,
                ) as websocket:
                    params: dict[str, Any] = {"channels": channels}
                    if market_tickers:
                        params["market_tickers"] = market_tickers
                    await websocket.send(
                        json.dumps({"id": 1, "cmd": "subscribe", "params": params})
                    )

                    backoff = 1.0
                    async for raw in websocket:
                        data = json.loads(raw)
                        msg_type = str(data.get("type") or "unknown")
                        if msg_type == "error":
                            message = data.get("msg", {})
                            raise RuntimeError(
                                f"Kalshi WebSocket error "
                                f"{message.get('code')}: {message.get('msg')}"
                            )
                        yield KalshiStreamMessage(type=msg_type, payload=data)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not reconnect:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
