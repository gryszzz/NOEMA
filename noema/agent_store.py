from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .agent_identity import AgentIdentity
from .agent_models import AgentConnectionState, AgentCycleState, AgentStatus


class AgentStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
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
        self.conn.commit()

    def write_status(self, status: AgentStatus) -> None:
        payload = json.dumps(asdict(status), default=str, sort_keys=True)
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

    def append_heartbeat(self, status: AgentStatus) -> None:
        cycle = status.last_cycle
        payload = json.dumps(asdict(status), default=str, sort_keys=True)
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
                note=last_cycle_raw.get("note"),
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
