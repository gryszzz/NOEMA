"""Bounded, read-only operational projection. Never constructs a writable Store."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paper_execution import parse_aware_time

# Identifiers below are application constants, never request parameters.
SECTIONS = {
    "queue": ("research_queue", "task_id,market_id,request,priority,status,created_at", "created_at"),
    "specialists": ("ecosystem_specialists", ("name,family,state,resolved,reliability,"
                    "calibration_error,after_cost_return,drawdown_fraction,updated_at"), "updated_at"),
    "experiments": ("research_trials", ("trial_id,family,hypothesis,feature_set_version,status,"
                    "created_at,parent_trial_id"), "created_at"),
    "decisions": ("forecast_ledger", "id,created_at,venue,market_id,forecast_json,action_json", "id"),
    "reviews": ("specialist_evolution_reviews", "id,specialist,created_at,previous_state,next_state",
                "id"),
    "reservations": ("cognition_budget_reservations", ("id,day_utc,estimated_usd,created_at,"
                     "estimated_tokens,market_id"), "id"),
    "outcomes": ("outcomes", "venue,market_id,outcome_yes,resolved_at,first_seen_at", "first_seen_at"),
}


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value[:4000] if isinstance(value, str) else None


def _object(value: str) -> dict:
    if len(value) > 131072:
        raise ValueError("oversized record")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("invalid record")
    return parsed


def build_operations(path: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    result: dict[str, Any] = {
        "as_of": now.isoformat(), "database_present": Path(path).is_file(),
        "runtime": {"state": "unknown", "last_heartbeat_at": None}, "sections": {},
    }
    for name in SECTIONS:
        result["sections"][name] = {"status": "not_recorded", "rows": [], "has_more": False}
    if not result["database_present"]:
        return result
    conn = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "agent_runtime" in tables:
            try:
                row = conn.execute(
                    "SELECT status_json FROM agent_runtime WHERE agent_id='noema'"
                ).fetchone()
                if row:
                    status = _object(row[0])
                    heartbeat = parse_aware_time(status["last_heartbeat_at"])
                    age = (now - heartbeat).total_seconds()
                    state = "unknown" if age < 0 else (
                        "stale" if age > 90 else
                        "running" if status.get("running") is True else "stopped"
                    )
                    result["runtime"] = {
                        "state": state, "last_heartbeat_at": heartbeat.isoformat(),
                    }
                    cycle = status.get("last_cycle")
                    if isinstance(cycle, dict):
                        result["runtime"]["cycle"] = {
                            key: _scalar(cycle.get(key))
                            for key in ("cycle_id", "active_goal", "health", "started_at", "completed_at")
                        }
                        result["runtime"]["connections"] = {
                            key: _scalar(cycle[key].get("status"))
                            for key in ("market_data", "kalshi", "cognition", "trench", "evm_wallet")
                            if isinstance(cycle.get(key), dict)
                        }
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                result["runtime"]["state"] = "invalid"
        for name, (table, columns, order) in SECTIONS.items():
            if table not in tables:
                continue
            section = result["sections"][name]
            try:
                rows = conn.execute(
                    f"SELECT {columns} FROM {table} ORDER BY {order} DESC LIMIT 51"
                ).fetchall()
                section["has_more"] = len(rows) > 50
                for row in rows[:50]:
                    record = {key: _scalar(row[key]) for key in dict(row) if not key.endswith('_json')}
                    if name == "decisions":
                        try:
                            forecast, action = _object(row["forecast_json"]), _object(row["action_json"])
                            for key in ("model_version", "probability_yes", "lower_bound", "upper_bound"):
                                record[key] = _scalar(forecast.get(key))
                            for key in ("decision", "reason"):
                                record[key] = _scalar(action.get(key))
                        except (ValueError, TypeError):
                            record["record_status"] = "invalid"
                    section["rows"].append(record)
                section["status"] = "recorded" if rows else "empty"
            except sqlite3.Error:
                section["status"] = "unavailable"
    except sqlite3.Error:
        result["runtime"]["state"] = "unavailable"
        for section in result["sections"].values():
            section["status"] = "unavailable"
    finally:
        if conn is not None:
            conn.close()
    return result
