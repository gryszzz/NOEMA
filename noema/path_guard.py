"""Outermost request-path guard for the console.

Rejects path traversal and path-smuggling forms before routing, auth allowlists or static file
handlers see the request: ``.``/``..`` segments (raw or percent-encoded), encoded slashes or
backslashes, literal backslashes and NUL bytes. Legitimate console URLs never contain these.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_ENCODED = ("%2e", "%2f", "%5c", "%00")


def is_suspicious_path(raw: bytes | str, decoded: str = "") -> bool:
    text = raw.decode("latin-1") if isinstance(raw, bytes) else raw
    for candidate in (text, decoded):
        if not candidate:
            continue
        if "\\" in candidate or "\x00" in candidate:
            return True
        if any(seg in (".", "..") for seg in candidate.split("/")):
            return True
    low = text.lower()
    return any(token in low for token in _ENCODED)


class PathGuardMiddleware:
    """Pure ASGI middleware so it runs before every other layer when added last."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            raw = scope.get("raw_path") or scope.get("path", "").encode("utf-8", "surrogateescape")
            if is_suspicious_path(raw, scope.get("path", "")):
                print(json.dumps({"event": "path_guard_reject", "method": scope.get("method"),
                                  "path": raw.decode("latin-1")[:200]}), file=sys.stderr, flush=True)
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = b'{"detail":"invalid path"}'
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
