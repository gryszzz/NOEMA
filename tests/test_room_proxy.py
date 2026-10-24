import base64

import httpx
import pytest
from starlette.testclient import TestClient

from noema import room_proxy


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("NOEMA_RUN_DIR", str(tmp_path))
    monkeypatch.delenv("NOEMA_CONSOLE_USERNAME", raising=False)
    monkeypatch.delenv("NOEMA_CONSOLE_PASSWORD", raising=False)
    seen = {}

    def handler(req):
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, stream=httpx.ByteStream(b"dashboard"))

    room_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = TestClient(room_proxy.app, client=("127.0.0.1", 5555))
    c.seen = seen
    yield c
    room_proxy._client = None


def test_owner_gets_dashboard_without_password(client):
    r = client.get("/trading-room", headers={"Tailscale-User-Login": "gryszzz@github"})
    assert r.status_code == 200 and r.text == "dashboard"
    assert "www-authenticate" not in r.headers
    assert not any(k.startswith("tailscale-") for k in client.seen["headers"])


def test_no_identity_gets_login(client):
    r = client.get("/trading-room")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]


def test_other_login_and_funnel_rejected(client):
    assert client.get("/", headers={"Tailscale-User-Login": "eve@github"}).status_code == 401
    r = client.get("/", headers={"Tailscale-User-Login": "gryszzz@github", "Tailscale-Funnel-Request": "?1"})
    assert r.status_code == 401


def test_non_loopback_peer_rejected(client):
    c = TestClient(room_proxy.app, client=("100.64.0.9", 5555))
    assert c.get("/", headers={"Tailscale-User-Login": "gryszzz@github"}).status_code == 401


def test_password_still_works(client, tmp_path):
    _, pw = room_proxy._credentials()
    assert (tmp_path / "room-proxy.secret").stat().st_mode & 0o077 == 0
    auth = "Basic " + base64.b64encode(f"tony:{pw}".encode()).decode()
    assert client.get("/", headers={"Authorization": auth}).status_code == 200
    assert "authorization" not in client.seen["headers"]
    bad = "Basic " + base64.b64encode(b"tony:nope").decode()
    assert client.get("/", headers={"Authorization": bad}).status_code == 401


def test_launch_script_disables_uvicorn_proxy_headers():
    """uvicorn must not rewrite client.host from X-Forwarded-For, or the
    loopback check sees the tailnet IP and the owner gets a 401."""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "scripts" / "run-room-proxy.sh").read_text()
    assert "--no-proxy-headers" in script
