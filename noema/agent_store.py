from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .agent_identity import AgentIdentity
from .agent_models import AgentConnectionState, AgentCycleState, AgentStatus


class AgentStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        self.path = str(db.resolve())
        self.process_identity: dict[str, object] | None = None
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runtime (
                agent_id TEXT PRIMARY KEY,
                status_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                cycle_id INTEGER,
                health TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_cycle_timings (
                cycle_id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                stage_timings_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def write_status(self, status: AgentStatus) -> None:
        status_payload = asdict(status)
        if status.running and self.process_identity is not None:
            status_payload["process_identity"] = self.process_identity
        payload = json.dumps(status_payload, default=str, sort_keys=True)
        self.conn.execute(
            """
            INSERT INTO agent_runtime (agent_id, status_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                status_json = excluded.status_json,
                updated_at = excluded.updated_at
            """,
            (status.agent_id, payload, datetime.now(UTC).isoformat()),
        )
        self.conn.commit()

    def set_process_identity(self, identity: dict[str, object]) -> None:
        self.process_identity = identity

    def read_process_identity(self, agent_id: str = "noema") -> dict[str, object] | None:
        row = self.conn.execute(
            "SELECT status_json FROM agent_runtime WHERE agent_id = ?", (agent_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        identity = payload.get("process_identity") if isinstance(payload, dict) else None
        return identity if isinstance(identity, dict) else None

    def append_heartbeat(self, status: AgentStatus) -> None:
        cycle = status.last_cycle
        heartbeat_payload = asdict(status)
        if status.running and self.process_identity is not None:
            heartbeat_payload["process_identity"] = self.process_identity
        payload = json.dumps(heartbeat_payload, default=str, sort_keys=True)
        self.conn.execute(
            """
            INSERT INTO agent_heartbeats
            (agent_id, created_at, cycle_id, health, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                status.agent_id,
                datetime.now(UTC).isoformat(),
                None if cycle is None else cycle.cycle_id,
                "starting" if cycle is None else cycle.health,
                payload,
            ),
        )
        self.conn.commit()

    def append_cycle_timing(
        self, *, cycle_id: int, started_at: datetime, completed_at: datetime,
        duration_seconds: float, stage_timings: dict[str, float],
    ) -> None:
        """Persist final stage timing for one cycle; repeated writes are idempotent."""
        values = {str(name): float(value) for name, value in stage_timings.items()}
        if (not math.isfinite(duration_seconds) or duration_seconds < 0
                or any(not math.isfinite(value) or value < 0 for value in values.values())):
            raise ValueError("cycle timings must be finite, nonnegative seconds")
        self.conn.execute(
            """
            INSERT INTO agent_cycle_timings
            (cycle_id, started_at, completed_at, duration_seconds, stage_timings_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id) DO UPDATE SET
                started_at=excluded.started_at, completed_at=excluded.completed_at,
                duration_seconds=excluded.duration_seconds,
                stage_timings_json=excluded.stage_timings_json
            """,
            (cycle_id, started_at.isoformat(), completed_at.isoformat(), duration_seconds,
             json.dumps(values, sort_keys=True)),
        )
        self.conn.commit()

    def refresh_heartbeat(self, at: datetime | None = None) -> bool:
        """Refresh only heartbeat fields, preserving any concurrently completed cycle."""
        at = at or datetime.now(UTC)
        self.conn.execute("BEGIN IMMEDIATE")
        row = self.conn.execute(
            "SELECT status_json FROM agent_runtime WHERE agent_id='noema'"
        ).fetchone()
        if row is None:
            self.conn.commit()
            return False
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            self.conn.commit()
            return False
        if not isinstance(payload, dict) or not payload.get("running"):
            self.conn.commit()
            return False
        if payload.get("process_identity") != self.process_identity:
            self.conn.commit()
            return False

        payload["last_heartbeat_at"] = at.isoformat()
        encoded = json.dumps(payload, default=str, sort_keys=True)
        self.conn.execute(
            "UPDATE agent_runtime SET status_json=?,updated_at=? WHERE agent_id='noema'",
            (encoded, at.isoformat()),
        )
        cycle = payload.get("last_cycle")
        cycle_id = cycle.get("cycle_id") if isinstance(cycle, dict) else None
        health = cycle.get("health", "starting") if isinstance(cycle, dict) else "starting"
        self.conn.execute(
            "INSERT INTO agent_heartbeats"
            " (agent_id,created_at,cycle_id,health,payload_json) VALUES (?,?,?,?,?)",
            ("noema", at.isoformat(), cycle_id, health, encoded),
        )
        self.conn.commit()
        return True

    def append_runtime_event(
        self, session_id: str, stage: str, status: str, detail: str,
    ) -> None:
        """Persist a small lifecycle/cycle marker in the workstation event feed."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                tool TEXT,
                evidence_id TEXT,
                elapsed_seconds REAL,
                cost_usd REAL,
                detail TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO runtime_events
            (session_id, created_at, stage, status, tool, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, datetime.now(UTC).isoformat(), stage, status,
             "noema-agent", detail),
        )
        self.conn.commit()

    def read_status(self, identity: AgentIdentity | None = None) -> AgentStatus:
        identity = identity or AgentIdentity()
        row = self.conn.execute(
            "SELECT status_json, updated_at FROM agent_runtime WHERE agent_id = ?",
            (identity.agent_id,),
        ).fetchone()
        if row is None:
            return AgentStatus(
                agent_id=identity.agent_id,
                name=identity.name,
                version=identity.version,
                mission=identity.mission,
                running=False,
                last_heartbeat_at=None,
                last_cycle=None,
            )

        raw = json.loads(row[0])
        last_cycle_raw = raw.get("last_cycle")
        last_cycle = None
        if last_cycle_raw:
            last_cycle = AgentCycleState(
                cycle_id=int(last_cycle_raw["cycle_id"]),
                started_at=datetime.fromisoformat(last_cycle_raw["started_at"]),
                completed_at=(
                    None
                    if last_cycle_raw.get("completed_at") is None
                    else datetime.fromisoformat(last_cycle_raw["completed_at"])
                ),
                active_goal=str(last_cycle_raw["active_goal"]),
                health=str(last_cycle_raw["health"]),
                kalshi=AgentConnectionState(**last_cycle_raw["kalshi"]),
                evm_wallet=AgentConnectionState(**last_cycle_raw["evm_wallet"]),
                radar_markets=int(last_cycle_raw["radar_markets"]),
                economic_state=str(last_cycle_raw["economic_state"]),
                ecosystem_state=str(last_cycle_raw.get("ecosystem_state", "uninitialized")),
                ecosystem_focus=last_cycle_raw.get("ecosystem_focus"),
                cognition=AgentConnectionState(
                    **last_cycle_raw.get(
                        "cognition",
                        {"status": "unconfigured", "detail": None},
                    )
                ),
                note=last_cycle_raw.get("note"),
                market_data=AgentConnectionState(
                    **last_cycle_raw.get(
                        "market_data",
                        {"status": "unconfigured", "detail": "cycle predates market health tracking"},
                    )
                ),
                trench=AgentConnectionState(
                    **last_cycle_raw.get(
                        "trench",
                        {"status": "disabled", "detail": "cycle predates Trench collection"},
                    )
                ),
                polymarket_us=AgentConnectionState(
                    **last_cycle_raw.get(
                        "polymarket_us",
                        {"status": "unconfigured", "detail": "cycle predates Polymarket US collection"},
                    )
                ),
                duration_seconds=(
                    None if last_cycle_raw.get("duration_seconds") is None
                    else float(last_cycle_raw["duration_seconds"])
                ),
                cadence_seconds=(
                    None if last_cycle_raw.get("cadence_seconds") is None
                    else float(last_cycle_raw["cadence_seconds"])
                ),
                stage_timings={
                    str(key): float(value)
                    for key, value in (last_cycle_raw.get("stage_timings") or {}).items()
                    if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) >= 0
                },
            )

        return AgentStatus(
            agent_id=str(raw["agent_id"]),
            name=str(raw["name"]),
            version=str(raw["version"]),
            mission=str(raw["mission"]),
            running=bool(raw["running"]),
            last_heartbeat_at=datetime.fromisoformat(row[1]),
            last_cycle=last_cycle,
        )
