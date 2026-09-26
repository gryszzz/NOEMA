from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .soak_report import build_soak_quality_report


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _safe_scalar(conn: sqlite3.Connection, sql: str, default: float = 0.0) -> float:
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return default
    if row is None or row[0] is None:
        return default
    return float(row[0])


def build_overview(path: str = "data/noema.db") -> dict[str, Any]:
    if not Path(path).exists():
        return {
            "database_present": False,
            "soak": asdict(build_soak_quality_report(path)),
            "forecasts": 0,
            "resolved_markets": 0,
            "model_trust": [],
            "realtime_events": 0,
            "evidence_records": 0,
        }

    conn = sqlite3.connect(path)
    overview: dict[str, Any] = {
        "database_present": True,
        "soak": asdict(build_soak_quality_report(path)),
        "forecasts": (
            int(_safe_scalar(conn, "SELECT COUNT(*) FROM forecast_ledger"))
            if _table_exists(conn, "forecast_ledger")
            else 0
        ),
        "resolved_markets": (
            int(_safe_scalar(conn, "SELECT COUNT(*) FROM outcomes"))
            if _table_exists(conn, "outcomes")
            else 0
        ),
        "realtime_events": (
            int(_safe_scalar(conn, "SELECT COUNT(*) FROM realtime_events"))
            if _table_exists(conn, "realtime_events")
            else 0
        ),
        "evidence_records": (
            int(_safe_scalar(conn, "SELECT COUNT(*) FROM evidence_records"))
            if _table_exists(conn, "evidence_records")
            else 0
        ),
    }

    trust_rows: list[dict[str, Any]] = []
    if _table_exists(conn, "model_trust"):
        for model_name, state_json in conn.execute(
            "SELECT model_name, state_json FROM model_trust ORDER BY model_name"
        ):
            payload = json.loads(state_json)
            trust_rows.append(
                {
                    "model_name": model_name,
                    "resolved": payload.get("resolved", 0),
                    "log_weight": payload.get("log_weight", 0.0),
                }
            )
    overview["model_trust"] = trust_rows
    return overview
