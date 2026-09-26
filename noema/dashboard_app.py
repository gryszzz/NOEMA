from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .agent_dashboard import build_agent_overview
from .cognition_dashboard import build_cognition_overview
from .dashboard_data import build_overview
from .doctor import doctor_report
from .economic_dashboard import build_economic_overview
from .kalshi_telemetry import KalshiTelemetry
from .ladder import build_ladder_report
from .opportunity_radar import build_radar
from .paired_evaluation import compare_history_to_market
from .telemetry_report import build_telemetry_report
from .wallet_diagnostics import public_wallet_policy

app = FastAPI(title="NOEMA Ops Console", docs_url="/docs", redoc_url=None)


def _db_path() -> str:
    return os.getenv("NOEMA_DB_PATH", "data/noema.db")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    path = Path(__file__).with_name("static") / "index.html"
    return path.read_text()


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


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    return build_overview(_db_path())


@app.get("/api/economy")
async def economy() -> dict[str, Any]:
    return build_economic_overview(_db_path())


@app.get("/api/radar")
async def radar() -> list[dict[str, Any]]:
    return [row.__dict__ for row in build_radar(_db_path())]


@app.get("/api/wallet-policy")
async def wallet_policy() -> dict[str, Any]:
    return public_wallet_policy()


@app.get("/api/live-account")
async def live_account() -> dict[str, Any]:
    try:
        telemetry = KalshiTelemetry()
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
