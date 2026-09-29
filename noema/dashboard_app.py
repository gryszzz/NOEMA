from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent_dashboard import build_agent_overview
from .bill_tracker import BillTracker
from .cognition_dashboard import build_cognition_overview, build_provider_health
from .config import kalshi_production_read_only_config
from .dashboard_data import build_overview
from .doctor import doctor_report
from .economic_dashboard import build_economic_overview
from .economic_measurement import build_economic_measurement
from .ecosystem_dashboard import build_ecosystem_overview
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

app = FastAPI(title="NOEMA Ops Console", docs_url="/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
_wallet_status_cache: dict[str, Any] = {"fetched_at": 0.0, "networks": []}
_wallet_status_lock = asyncio.Lock()


def _db_path() -> str:
    return os.getenv("NOEMA_DB_PATH", "data/noema.db")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    path = Path(__file__).with_name("static") / "index.html"
    return path.read_text()


async def _runtime_change_stream(request: Request):
    """Push invalidations only after another runtime connection commits SQLite state."""
    path = Path(_db_path()).resolve()
    if not path.is_file():
        yield "event: unavailable\ndata: {}\n\n"
        return
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("PRAGMA query_only=ON")
        version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        yield "event: ready\ndata: {}\n\n"
        last_keepalive = time.monotonic()
        while not await request.is_disconnected():
            await asyncio.sleep(1)
            current = int(conn.execute("PRAGMA data_version").fetchone()[0])
            if current != version:
                version = current
                yield "event: change\ndata: {}\n\n"
                last_keepalive = time.monotonic()
            elif time.monotonic() - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()
    except sqlite3.Error:
        yield "event: unavailable\ndata: {}\n\n"
    finally:
        conn.close()


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
    return build_cognition_overview(_db_path())


@app.get("/api/provider-health")
def provider_health() -> dict[str, Any]:
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


@app.get("/api/wallet-status")
async def wallet_status() -> dict[str, Any]:
    now = time.monotonic()
    if now - float(_wallet_status_cache["fetched_at"]) > 60:
        async with _wallet_status_lock:
            now = time.monotonic()
            if now - float(_wallet_status_cache["fetched_at"]) > 60:
                _wallet_status_cache["networks"] = await live_wallet_networks()
                _wallet_status_cache["fetched_at"] = time.monotonic()
    return {
        "as_of_monotonic": _wallet_status_cache["fetched_at"],
        "networks": _wallet_status_cache["networks"],
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
