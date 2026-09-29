from __future__ import annotations

import asyncio
import gzip
import os
import secrets
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent_dashboard import build_agent_overview
from .bill_tracker import BillTracker
from .cognition_dashboard import build_cognition_overview, build_provider_health
from .config import kalshi_production_read_only_config
from .console_replication import (
    decode_worker_metadata,
    load_worker_metadata,
    persist_worker_metadata,
)
from .dashboard_data import build_overview
from .doctor import doctor_report
from .economic_dashboard import build_economic_overview
from .economic_measurement import build_economic_measurement
from .ecosystem_dashboard import build_ecosystem_overview
from .execution_gateway import ExecutionGateway
from .kalshi_telemetry import KalshiTelemetry
from .knowledge import build_knowledge_overview
from .ladder import build_ladder_report
from .operations_dashboard import build_operations
from .opportunity_radar import build_radar
from .paired_evaluation import compare_history_to_market
from .prediction_venues import build_prediction_venue_status
from .stripe_economy import stripe_economy_overview
from .telemetry_report import build_telemetry_report
from .trench_dashboard import build_trench_overview
from .wallet_diagnostics import live_wallet_networks, public_wallet_policy

app = FastAPI(title="NOEMA Ops Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
_wallet_status_cache: dict[str, Any] = {"fetched_at": 0.0, "networks": []}
_wallet_status_lock = asyncio.Lock()
_MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
_MAX_DATABASE_BYTES = 512 * 1024 * 1024


def _constant_time_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@app.middleware("http")
async def protect_console(request: Request, call_next):
    """Protect every console surface and accept worker replication on its own token."""
    path = request.url.path
    if path == "/healthz":
        return await call_next(request)
    if path == "/internal/snapshot":
        configured = os.getenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "")
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {configured}" if configured else ""
        if not configured:
            return JSONResponse({"detail": "snapshot receiver is not configured"}, status_code=503)
        if not _constant_time_equal(supplied, expected):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    username = os.getenv("NOEMA_CONSOLE_USERNAME", "")
    password = os.getenv("NOEMA_CONSOLE_PASSWORD", "")
    if os.getenv("NOEMA_CONSOLE_AUTH_REQUIRED") == "1" and (not username or not password):
        return JSONResponse({"detail": "console authentication is not configured"}, status_code=503)
    if username or password:
        supplied = request.headers.get("authorization", "")
        valid = False
        if supplied.startswith("Basic ") and username and password:
            import base64

            try:
                decoded = base64.b64decode(supplied[6:], validate=True).decode("utf-8")
                supplied_user, separator, supplied_password = decoded.partition(":")
                valid = bool(separator) and _constant_time_equal(supplied_user, username)
                valid = valid and _constant_time_equal(supplied_password, password)
            except (ValueError, UnicodeDecodeError):
                valid = False
        if not valid:
            return JSONResponse(
                {"detail": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="NOEMA Operations", charset="UTF-8"'},
            )
    return await call_next(request)


def _db_path() -> str:
    return os.getenv("NOEMA_DB_PATH", "data/noema.db")


def _worker_provider_health() -> dict[str, Any]:
    metadata = load_worker_metadata(_db_path())
    if metadata is None:
        return {
            "source": "worker snapshot metadata unavailable",
            "configured_provider": "unknown",
            "configured_provider_ready": None,
            "connectivity": "not probed by console",
        }
    cognition = metadata["cognition"]
    provider = str(cognition["provider"])
    configured = cognition["configured"]
    status = ("configuration_ready_connectivity_unverified" if configured is True
              else "not_ready_or_unconfigured" if configured is False else "unknown")
    provider_rows = {
        name: {"status": status if provider == name else "not_current_worker_route",
               "model": cognition["deployment"] if provider == name else None,
               "credential_present": None}
        for name in ("openai", "cloudflare_workers_ai", "foundry", "docker_model_runner")
    }
    provider_rows.update({
        "groq": {"status": "not_current_worker_route", "credential_present": None},
        "chronos": {"status": "not_probed_by_console"},
        "finbert": {"status": "not_probed_by_console"},
        "specialists": {"status": "not_probed_by_console"},
        "local_model_runner": {"status": "not_probed_by_console"},
    })
    return {
        **provider_rows,
        "source": "worker environment metadata paired with latest database snapshot",
        "configured_provider": provider,
        "configured_provider_ready": configured,
        "connectivity": "not probed by console",
        "worker_commit": metadata["worker_commit"],
    }


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Render health check; it reveals no operational data or credentials."""
    path = Path(_db_path())
    return {
        "status": "ready" if path.is_file() else "awaiting_worker_snapshot",
        "database_present": path.is_file(),
    }


@app.get("/api/runtime")
def runtime_info() -> dict[str, Any]:
    path = Path(_db_path())
    try:
        stat = path.stat()
    except OSError:
        return {
            "database_present": False,
            "source": os.getenv("NOEMA_RUNTIME_SOURCE", "local"),
            "deployment_commit": os.getenv("RENDER_GIT_COMMIT"),
            "worker": load_worker_metadata(_db_path()),
        }
    age_seconds = max(0, int(time.time() - stat.st_mtime))
    stale_after_seconds = max(900, 3 * int(os.getenv("NOEMA_AGENT_CYCLE_SECONDS", "300")))
    return {
        "database_present": True,
        "source": os.getenv("NOEMA_RUNTIME_SOURCE", "local"),
        "snapshot_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "age_seconds": age_seconds,
        "snapshot_stale": age_seconds > stale_after_seconds,
        "deployment_commit": os.getenv("RENDER_GIT_COMMIT"),
        "worker": load_worker_metadata(_db_path()),
        "database_bytes": stat.st_size,
    }


@app.post("/internal/snapshot")
async def receive_worker_snapshot(request: Request) -> dict[str, Any]:
    """Atomically replace the console's read-only replica with a verified SQLite image."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_SNAPSHOT_BYTES:
                raise HTTPException(status_code=413, detail="snapshot is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
    compressed = await request.body()
    if len(compressed) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="snapshot is too large")
    try:
        inflater = gzip.GzipFile(fileobj=__import__("io").BytesIO(compressed))
        image = inflater.read(_MAX_DATABASE_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise HTTPException(status_code=400, detail="invalid snapshot encoding") from exc
    if len(image) > _MAX_DATABASE_BYTES:
        raise HTTPException(status_code=413, detail="database snapshot is too large")
    encoded_metadata = request.headers.get("x-noema-worker-metadata")
    try:
        worker_metadata = None if encoded_metadata is None else decode_worker_metadata(encoded_metadata)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="worker metadata is invalid") from exc

    destination = Path(_db_path()).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = temporary.name
            temporary.write(image)
            temporary.flush()
            os.fsync(temporary.fileno())
        uri = Path(temporary_path).resolve().as_uri() + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as conn:
                check = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=400, detail="snapshot is not a valid SQLite database") from exc
        else:
            if check is None or check[0] != "ok":
                raise HTTPException(status_code=400, detail="snapshot failed SQLite integrity check")
        os.chmod(temporary_path, 0o444)
        os.replace(temporary_path, destination)
        temporary_path = None
        if worker_metadata is not None:
            persist_worker_metadata(_db_path(), worker_metadata)
    except HTTPException:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=500, detail="snapshot could not be persisted") from exc
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
    return {"status": "persisted", "database_bytes": len(image),
            "received_at": datetime.now(UTC).isoformat()}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    path = Path(__file__).with_name("static") / "index.html"
    return path.read_text()


async def _runtime_change_stream(request: Request):
    """Push invalidations when a verified worker snapshot is atomically replaced."""
    path = Path(_db_path()).resolve()
    if not path.is_file():
        yield "event: unavailable\ndata: {}\n\n"
        return
    try:
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        yield "event: ready\ndata: {}\n\n"
        last_keepalive = time.monotonic()
        while not await request.is_disconnected():
            await asyncio.sleep(1)
            try:
                current_stat = path.stat()
                current = (current_stat.st_mtime_ns, current_stat.st_size)
            except FileNotFoundError:
                yield "event: unavailable\ndata: {}\n\n"
                return
            if current != version:
                version = current
                yield "event: change\ndata: {}\n\n"
                last_keepalive = time.monotonic()
            elif time.monotonic() - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()
    except OSError:
        yield "event: unavailable\ndata: {}\n\n"


@app.get("/api/runtime-stream")
async def runtime_stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _runtime_change_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/detailed", response_class=HTMLResponse)
async def detailed() -> str:
    return (Path(__file__).with_name("static") / "detailed.html").read_text()


@app.get("/api/operations")
def operations() -> dict[str, Any]:
    return build_operations(_db_path())


@app.get("/api/knowledge")
def knowledge() -> dict[str, Any]:
    return build_knowledge_overview(_db_path())


@app.get("/api/agent")
async def agent() -> dict[str, Any]:
    return build_agent_overview(_db_path())


@app.get("/api/doctor")
async def doctor() -> dict[str, Any]:
    return doctor_report(_db_path())


@app.get("/api/ladder")
async def ladder() -> dict[str, Any]:
    return build_ladder_report(_db_path())


@app.get("/api/compare")
async def compare() -> dict[str, Any]:
    return compare_history_to_market(_db_path()).as_dict()


@app.get("/api/cognition")
async def cognition() -> dict[str, Any]:
    replica = os.getenv("NOEMA_RUNTIME_SOURCE") == "worker-sqlite-replica"
    overview = build_cognition_overview(_db_path(), probe_runtime=not replica)
    if replica:
        metadata = load_worker_metadata(_db_path())
        if metadata is None:
            overview.update({"enabled": None, "configured": None,
                             "credential_present": None,
                             "runtime_providers": _worker_provider_health()})
        else:
            cognition_metadata = metadata["cognition"]
            overview.update({
                "enabled": cognition_metadata["enabled"],
                "configured": cognition_metadata["configured"],
                "provider": cognition_metadata["provider"],
                "deployment": cognition_metadata["deployment"],
                "credential_present": None,
                "runtime_providers": _worker_provider_health(),
                "worker_commit": metadata["worker_commit"],
            })
    return overview


@app.get("/api/provider-health")
def provider_health() -> dict[str, Any]:
    if os.getenv("NOEMA_RUNTIME_SOURCE") == "worker-sqlite-replica":
        return _worker_provider_health()
    return build_provider_health()


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    return build_overview(_db_path())


@app.get("/api/economy")
async def economy() -> dict[str, Any]:
    return build_economic_overview(_db_path())


@app.get("/api/stripe-economy")
def stripe_economy() -> dict[str, Any]:
    """Secret-free projection of the latest Stripe observations persisted by the agent."""
    return stripe_economy_overview(_db_path())


@app.get("/api/ecosystem")
async def ecosystem() -> dict[str, Any]:
    return build_ecosystem_overview(_db_path())


@app.get("/api/economic-measurement")
async def economic_measurement() -> dict[str, Any]:
    return build_economic_measurement(_db_path())


@app.get("/api/bill")
async def bill() -> dict[str, Any]:
    return BillTracker(_db_path()).overview()


@app.get("/api/radar")
async def radar() -> list[dict[str, Any]]:
    return [row.__dict__ for row in build_radar(_db_path())]


@app.get("/api/trench")
async def trench() -> dict[str, Any]:
    return build_trench_overview(_db_path())


@app.get("/api/wallet-policy")
async def wallet_policy() -> dict[str, Any]:
    return public_wallet_policy()


@app.get("/api/execution-gateway")
async def execution_gateway_status() -> dict[str, Any]:
    return ExecutionGateway(_db_path(), initialize=False).overview()


@app.get("/api/wallet-status")
async def wallet_status() -> dict[str, Any]:
    now = time.monotonic()
    if now - float(_wallet_status_cache["fetched_at"]) > 60:
        async with _wallet_status_lock:
            now = time.monotonic()
            if now - float(_wallet_status_cache["fetched_at"]) > 60:
                _wallet_status_cache["networks"] = await live_wallet_networks()
                _wallet_status_cache["fetched_at"] = time.monotonic()
    policy = public_wallet_policy()
    policy_by_chain = {row["chain"]: row for row in policy.get("wallet_networks", [])}
    networks = []
    for cached in _wallet_status_cache["networks"]:
        row = dict(cached)
        current = policy_by_chain.get(row.get("chain"), {})
        # Balance/connection evidence is briefly cached; authority/configuration
        # flags are recomputed on every request so stale settings cannot appear live.
        for field in (
            "signer_configured", "credentials_isolated", "signer_process_enabled", "halted",
            "mission_authority_present", "live_execution_enabled", "coordinator_wired",
        ):
            if field in current:
                row[field] = current[field]
        row["signing_enabled"] = False
        networks.append(row)
    return {
        "as_of_monotonic": _wallet_status_cache["fetched_at"],
        "control_plane": {
            "live_execution_enabled": False,
            "mission_authority_present": False,
            "coordinator_wired": False,
            "halted": policy["master_halt"],
            "status": "SIGNER MAY BE CONFIGURED · LIVE EXECUTION DISABLED · COORDINATOR NOT WIRED",
        },
        "networks": networks,
    }


@app.get("/api/live-account")
async def live_account() -> dict[str, Any]:
    try:
        telemetry = KalshiTelemetry(kalshi_production_read_only_config())
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Kalshi credentials are not configured for read-only telemetry.",
        ) from exc

    try:
        orders, fills, positions = await __import__("asyncio").gather(
            telemetry.orders(),
            telemetry.fills(),
            telemetry.positions(),
        )
        return build_telemetry_report(
            orders=orders,
            fills=fills,
            positions=positions,
        )
    finally:
        await telemetry.close()


@app.get("/api/prediction-venues")
async def prediction_venues() -> dict[str, Any]:
    """Current official read-only status for supported prediction venues."""
    return await build_prediction_venue_status()
