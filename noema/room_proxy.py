"""TEV room proxy: one-click dashboard access for the owner over Tailscale.

Path in:  Mac TEV.app -> https://<node>.<tailnet>.ts.net  (tailscale serve, HTTPS)
          -> 127.0.0.1:NOEMA_ROOM_PROXY_PORT (this proxy) -> 127.0.0.1:8787 (dashboard).

Security model
* Listens on loopback only; `tailscale serve` is the only way in from outside.
* `tailscale serve` sets Tailscale-User-Login / -Name / -Profile-Pic itself and
  removes any copies the client sent, for tailnet (non-Funnel) traffic.
* The bypass applies only when the peer is loopback, the request did NOT come via
  Funnel (Tailscale-Funnel-Request absent) and Tailscale-User-Login equals one of
  NOEMA_ROOM_PROXY_TRUSTED_LOGINS (default gryszzz@github). The trusted path never
  sees a login page, cookie prompt or basic-auth dialog.
* Everyone else must pass HTTP Basic auth (NOEMA_CONSOLE_USERNAME/PASSWORD, or a
  random password kept in $NOEMA_RUN_DIR/room-proxy.secret, mode 600).
* All Tailscale-* identity headers and Authorization are stripped before the
  request is forwarded; the upstream gets the console credentials only when set.
Secrets are never logged.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import os
import secrets
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("room_proxy")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


def trusted_logins() -> set[str]:
    raw = os.getenv("NOEMA_ROOM_PROXY_TRUSTED_LOGINS", "gryszzz@github")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def upstream() -> str:
    return os.getenv("NOEMA_ROOM_PROXY_UPSTREAM", "http://127.0.0.1:8787").rstrip("/")


def _credentials() -> tuple[str, str]:
    user = os.getenv("NOEMA_CONSOLE_USERNAME", "")
    pw = os.getenv("NOEMA_CONSOLE_PASSWORD", "")
    if user and pw:
        return user, pw
    path = Path(os.getenv("NOEMA_RUN_DIR", "/workspace/noema-run")) / "room-proxy.secret"
    if not path.exists():
        old = os.umask(0o077)
        try:
            path.write_text(secrets.token_urlsafe(24) + "\n")
        finally:
            os.umask(old)
    return user or "tony", path.read_text().strip()


def _loopback(request: Request) -> bool:
    try:
        return ipaddress.ip_address((request.client.host if request.client else "") or "0.0.0.0").is_loopback
    except ValueError:
        return False


def is_trusted(request: Request) -> bool:
    if not _loopback(request) or "tailscale-funnel-request" in request.headers:
        return False
    logins = request.headers.getlist("tailscale-user-login")
    return len(logins) == 1 and logins[0].strip().lower() in trusted_logins()


def _basic_ok(request: Request) -> bool:
    supplied = request.headers.get("authorization", "")
    if not supplied.startswith("Basic "):
        return False
    try:
        u, sep, p = base64.b64decode(supplied[6:], validate=True).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    user, pw = _credentials()
    return bool(sep) and hmac.compare_digest(u, user) & hmac.compare_digest(p, pw)


_client: httpx.AsyncClient | None = None


async def proxy(request: Request):
    global _client
    if request.url.path == "/room-proxy/health":
        return PlainTextResponse("ok")
    trusted = is_trusted(request)
    if not trusted and not _basic_ok(request):
        log.info("401 %s %s", request.method, request.url.path)
        return PlainTextResponse("unauthorized", status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="TEV", charset="UTF-8"'})
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP and not k.lower().startswith("tailscale-") and k.lower() != "authorization"}
    cu, cp = os.getenv("NOEMA_CONSOLE_USERNAME", ""), os.getenv("NOEMA_CONSOLE_PASSWORD", "")
    if cu and cp:
        headers["authorization"] = "Basic " + base64.b64encode(f"{cu}:{cp}".encode()).decode()
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    url = upstream() + request.url.path + (("?" + request.url.query) if request.url.query else "")
    req = _client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        resp = await _client.send(req, stream=True)
    except httpx.HTTPError as exc:
        log.warning("upstream error %s", type(exc).__name__)
        return PlainTextResponse("dashboard unavailable", status_code=502)
    log.info("%s %s %s %s", resp.status_code, "owner" if trusted else "basic", request.method, request.url.path)
    out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP and k.lower() != "www-authenticate"}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out,
                             background=BackgroundTask(resp.aclose))


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
app = Starlette(routes=[Route("/{path:path}", proxy, methods=METHODS)])
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
