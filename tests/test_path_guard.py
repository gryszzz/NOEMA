from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from noema import dashboard_app
from noema.path_guard import is_suspicious_path

BAD = [
    "/api/live/../../api/agent/flags",
    "/api/live/%2e%2e/%2e%2e/api/agent/flags",
    "/api/live/%2E%2E/api/agent/flags",
    "/static/..%2fapi/agent/flags",
    "/static/..%5capi",
    "/static/./x.js",
    "/static/x%00.js",
    "/api\\agent\\flags",
]
GOOD = ["/", "/healthz", "/api/live", "/static/trading-room-v6.js", "/trading-room", "/api/x-signal"]


@pytest.mark.parametrize("path", BAD)
def test_flags_traversal_forms(path: str) -> None:
    assert is_suspicious_path(path)


@pytest.mark.parametrize("path", GOOD)
def test_normal_paths_pass(path: str) -> None:
    assert not is_suspicious_path(path)


@pytest.mark.parametrize("path", ["/api/live/../../api/agent/flags", "/api/live/%2e%2e/%2e%2e/api/agent/flags",
                                  "/static/..%2f..%2fprivate.env"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_console_rejects_traversal_before_routing(path: str, method: str, monkeypatch) -> None:
    monkeypatch.delenv("NOEMA_CONSOLE_USERNAME", raising=False)
    monkeypatch.delenv("NOEMA_CONSOLE_PASSWORD", raising=False)
    client = TestClient(dashboard_app.app)
    # The test client would normalise "..", so send the raw target through the ASGI scope.
    from noema.path_guard import PathGuardMiddleware

    seen: list[str] = []

    async def inner(scope, receive, send):  # pragma: no cover - must never be reached
        seen.append(scope["path"])

    import asyncio

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": method, "path": path.replace("%2e", ".").replace("%2f", "/"),
             "raw_path": path.encode(), "headers": [], "query_string": b""}
    asyncio.run(PathGuardMiddleware(inner)(scope, receive, send))
    assert not seen
    assert sent[0]["status"] == 400
    assert client.get("/healthz").status_code == 200


def test_guard_is_outermost_middleware() -> None:
    from noema.path_guard import PathGuardMiddleware

    assert dashboard_app.app.user_middleware[0].cls is PathGuardMiddleware


def test_post_api_live_is_not_a_route() -> None:
    client = TestClient(dashboard_app.app)
    assert client.post("/api/live", json={}).status_code in (401, 405)
