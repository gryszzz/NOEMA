"""Persistent mission coordination over NOEMA's existing trial and research stores."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MISSION_STATES = frozenset({
    "discovered", "queued", "claimed", "running", "waiting", "passed",
    "completed", "failed", "interrupted", "quarantined", "superseded",
})
HANDOFF_STATES = frozenset({
    "requested", "accepted", "running", "completed", "blocked", "failed", "declined",
})
CAPABILITIES = frozenset({
    "execute_allowlisted_research_handler", "read_research_result", "read_evidence_digest",
    "read_bounded_result", "return_structured_critique",
})
RESOURCE_KEYS = frozenset({
    "max_rows_per_table", "timeout_seconds", "network", "secrets", "live_execution",
    "inference", "financial_credentials", "signing_authority",
})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _grants(values: list[str], *, research_kind: str | None = None) -> str:
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError("explicit bounded capabilities are required")
    allowed = set(CAPABILITIES)
    if research_kind:
        allowed.add(f"read_frozen_evidence:{research_kind}")
    if any(not isinstance(item, str) or item not in allowed for item in values):
        raise ValueError("capability grant is outside the mission allowlist")
    return json.dumps(values, sort_keys=True)


def _resources(values: dict[str, Any]) -> str:
    if not isinstance(values, dict) or len(values) > 16 or set(values) - RESOURCE_KEYS:
        raise ValueError("resource grant is outside the mission allowlist")
    encoded = json.dumps(values, sort_keys=True, allow_nan=False)
    if len(encoded) > 2048:
        raise ValueError("resource grant exceeds its size bound")
    if values.get("live_execution") is True or values.get("signing_authority") is True:
        raise ValueError("mission grants cannot authorize live execution or signing")
    if values.get("network") not in {None, "denied"}:
        raise ValueError("research worker network access is denied")
    if values.get("secrets") not in {None, "none"}:
        raise ValueError("mission workers receive no secrets")
    if values.get("financial_credentials") not in {None, "none"}:
        raise ValueError("mission workers receive no financial credentials")
    return encoded


class MissionStore:
    """Mission and handoff ledger; workers, trials, sessions and lessons remain authoritative."""

    def __init__(self, path: str):
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS missions (
                mission_id TEXT PRIMARY KEY,
                trial_id TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                session_id TEXT,
                run_id INTEGER,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                specialist TEXT NOT NULL,
                capability_grants_json TEXT NOT NULL,
                resource_grant_json TEXT NOT NULL,
                result_json TEXT,
                lesson_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(trial_id,evidence_hash)
            );
            CREATE TABLE IF NOT EXISTS mission_events (
                id INTEGER PRIMARY KEY,
                mission_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mission_handoffs (
                handoff_id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                from_specialist TEXT NOT NULL,
                to_specialist TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                capability_grants_json TEXT NOT NULL,
                resource_grant_json TEXT NOT NULL,
                result_json TEXT
            );
            CREATE INDEX IF NOT EXISTS mission_events_by_mission
                ON mission_events(mission_id, id);
            CREATE INDEX IF NOT EXISTS mission_handoffs_by_mission
                ON mission_handoffs(mission_id, created_at);
        """)
        self.conn.commit()

    @staticmethod
    def mission_id(trial_id: str, evidence_hash: str) -> str:
        digest = hashlib.sha256(f"{trial_id}\n{evidence_hash}".encode()).hexdigest()[:24]
        return f"mission-{digest}"

    def discover(self, *, trial_id: str, evidence_hash: str, objective: str, specialist: str) -> str:
        if not trial_id.strip() or not evidence_hash.strip() or not objective.strip() or not specialist.strip():
            raise ValueError("mission discovery requires trial, objective and specialist")
        mission_id = self.mission_id(trial_id, evidence_hash)
        now = _now()
        with self.conn:
            exact = self.conn.execute(
                "SELECT mission_id,status FROM missions WHERE trial_id=? AND evidence_hash=?",
                (trial_id, evidence_hash),
            ).fetchone()
            if exact:
                if exact["status"] == "superseded":
                    self.conn.execute(
                        "UPDATE missions SET status='discovered',objective=?,specialist=?,updated_at=?,"
                        "completed_at=NULL WHERE mission_id=? AND status='superseded'",
                        (objective[:1000], specialist, now, exact["mission_id"]),
                    )
                    self._event(str(exact["mission_id"]), "NOEMA", "opportunity_rediscovered",
                                "discovered", "Previously superseded evidence became current again", {
                                    "trial_id": trial_id, "evidence_hash": evidence_hash,
                                })
                return str(exact["mission_id"])
            pending = self.conn.execute(
                "SELECT mission_id,evidence_hash FROM missions WHERE trial_id=? "
                "AND status IN ('discovered','queued') ORDER BY updated_at DESC LIMIT 1",
                (trial_id,),
            ).fetchone()
            if pending:
                stale = self.conn.execute(
                    "SELECT mission_id FROM missions WHERE trial_id=? AND mission_id<>? "
                    "AND status IN ('discovered','queued')",
                    (trial_id, pending["mission_id"]),
                ).fetchall()
                for row in stale:
                    old_id = str(row["mission_id"])
                    self.conn.execute(
                        "UPDATE missions SET status='superseded',updated_at=?,completed_at=? "
                        "WHERE mission_id=? AND status IN ('discovered','queued')",
                        (now, now, old_id),
                    )
                    self._event(old_id, "NOEMA", "mission_superseded", "superseded",
                                "Replaced by a newer evidence snapshot for the same opportunity", {
                                    "superseded_by": pending["mission_id"],
                                })
                self.conn.execute(
                    "UPDATE missions SET evidence_hash=?,objective=?,specialist=?,updated_at=? "
                    "WHERE mission_id=? AND status IN ('discovered','queued')",
                    (evidence_hash, objective[:1000], specialist, now, pending["mission_id"]),
                )
                self._event(str(pending["mission_id"]), "NOEMA", "evidence_refreshed", "discovered",
                            "Unclaimed mission refreshed with newer frozen evidence", {
                                "previous_evidence_hash": pending["evidence_hash"],
                                "evidence_hash": evidence_hash,
                            })
                return str(pending["mission_id"])
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO missions(mission_id,trial_id,evidence_hash,objective,status,specialist,"
                "capability_grants_json,resource_grant_json,created_at,updated_at) "
                "VALUES (?,?,?,?,'discovered',?,'[]','{}',?,?)",
                (mission_id, trial_id, evidence_hash, objective[:1000], specialist, now, now),
            )
            if cursor.rowcount:
                self._event(mission_id, "NOEMA", "opportunity_discovered", "discovered",
                            "Registered evidence-backed research opportunity", {"trial_id": trial_id})
        return mission_id

    def claim(self, mission_id: str, *, specialist: str, session_id: str, run_id: int,
              evidence_hash: str, capability_grants: list[str], resource_grant: dict[str, Any]) -> bool:
        capability_payload = _grants(
            capability_grants,
            research_kind=(capability_grants[0].split(":", 1)[1]
                           if isinstance(capability_grants, list) and capability_grants
                           and isinstance(capability_grants[0], str)
                           and capability_grants[0].startswith("read_frozen_evidence:") else None),
        )
        resource_payload = _resources(resource_grant)
        now = _now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cursor = self.conn.execute(
                "UPDATE missions SET status='claimed',specialist=?,session_id=?,run_id=?,"
                "evidence_hash=?,capability_grants_json=?,resource_grant_json=?,updated_at=? "
                "WHERE mission_id=? AND specialist=? AND status IN ('discovered','queued')",
                (specialist, session_id, run_id, evidence_hash,
                 capability_payload, resource_payload, now, mission_id, specialist),
            )
            if cursor.rowcount:
                self._event(mission_id, specialist, "claimed", "claimed",
                            "Specialist claimed bounded mission with explicit grants", {
                                "session_id": session_id, "run_id": run_id,
                                "capabilities": capability_grants, "resources": resource_grant,
                            })
            self.conn.commit()
            return cursor.rowcount == 1
        except BaseException:
            self.conn.rollback()
            raise

    def queue(self, mission_id: str, *, reason: str) -> None:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE missions SET status='queued',updated_at=? WHERE mission_id=? "
                "AND status='discovered'", (_now(), mission_id),
            )
            if cursor.rowcount:
                self._event(mission_id, "NOEMA", "resource_queue", "queued", reason[:1000], {})

    def transition(self, mission_id: str, *, actor: str, event_type: str, status: str,
                   detail: str, payload: dict[str, Any] | None = None) -> None:
        if status not in MISSION_STATES:
            raise ValueError("invalid mission state")
        now = _now()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE missions SET status=?,updated_at=?,completed_at=? WHERE mission_id=?",
                (status, now, now if status in {"passed", "completed", "failed", "quarantined", "superseded"} else None,
                 mission_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("mission does not exist")
            self._event(mission_id, actor, event_type, status, detail, payload or {})

    def attach_result(self, mission_id: str, result: dict[str, Any]) -> None:
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE missions SET result_json=?,updated_at=? WHERE mission_id=?",
                (encoded, _now(), mission_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("mission does not exist")

    def attach_lesson(self, mission_id: str, lesson_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE missions SET lesson_id=?,updated_at=? WHERE mission_id=?",
                              (lesson_id, _now(), mission_id))

    def record_wallet_event(self, mission_id: str, *, status: str, detail: str,
                            payload: dict[str, Any]) -> str | None:
        """Attach a sanitized signer receipt to its existing mission lineage."""
        with self.conn:
            row = self.conn.execute(
                "SELECT session_id,result_json FROM missions WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            if row is None:
                raise KeyError("wallet intent mission does not exist")
            self._event(mission_id, "NOEMA", "wallet_execution_" + status, status,
                        detail, payload)
            result = json.loads(row["result_json"] or "{}")
            if not isinstance(result, dict):
                result = {}
            result["wallet_execution"] = payload
            self.conn.execute(
                "UPDATE missions SET result_json=?,updated_at=? WHERE mission_id=?",
                (json.dumps(result, sort_keys=True, allow_nan=False), _now(), mission_id),
            )
            return row["session_id"]

    def current_status(self, mission_id: str) -> str | None:
        """Read current mission state; existence alone never conveys authority."""
        row = self.conn.execute(
            "SELECT status FROM missions WHERE mission_id=?", (mission_id,),
        ).fetchone()
        return None if row is None else str(row["status"])

    def record_observation(self, mission_id: str, *, actor: str, event_type: str,
                           detail: str, payload: dict[str, Any]) -> None:
        """Append an external observation without changing mission status or authority."""
        if not actor.strip() or not event_type.strip() or len(event_type) > 80:
            raise ValueError("observation actor and event type are required")
        json.dumps(payload, sort_keys=True, allow_nan=False)
        with self.conn:
            if not self.conn.execute(
                "SELECT 1 FROM missions WHERE mission_id=?", (mission_id,),
            ).fetchone():
                raise KeyError("mission does not exist")
            self._event(mission_id, actor[:80], event_type, "observed", detail, payload)

    def request_handoff(self, mission_id: str, *, from_specialist: str, to_specialist: str,
                        objective: str, capability_grants: list[str], resource_grant: dict[str, Any],
                        contract: dict[str, Any] | None = None) -> str:
        if not from_specialist.strip() or not to_specialist.strip() or not objective.strip():
            raise ValueError("handoff requires source, recipient and bounded objective")
        capability_payload = _grants(capability_grants)
        resource_payload = _resources(resource_grant)
        contract_payload = contract or {}
        if not isinstance(contract_payload, dict) or len(contract_payload) > 12:
            raise ValueError("handoff contract exceeds its shape bound")
        encoded_contract = json.dumps(contract_payload, sort_keys=True, allow_nan=False)
        if len(encoded_contract.encode("utf-8")) > 4096:
            raise ValueError("handoff contract exceeds its size bound")
        handoff_id, now = str(uuid.uuid4()), _now()
        with self.conn:
            exists = self.conn.execute("SELECT 1 FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
            if not exists:
                raise KeyError("mission does not exist")
            self.conn.execute(
                "INSERT INTO mission_handoffs VALUES (?,?,?,?,?,?,?,'requested',?,?,NULL)",
                (handoff_id, mission_id, now, now, from_specialist, to_specialist,
                 objective[:1000], capability_payload, resource_payload),
            )
            self.conn.execute(
                "UPDATE missions SET status='waiting',updated_at=? WHERE mission_id=? "
                "AND status IN ('claimed','running')", (now, mission_id),
            )
            self._event(mission_id, from_specialist, "handoff_requested", "waiting",
                        f"Handoff requested from {to_specialist}", {
                            "handoff_id": handoff_id, "contract": contract_payload,
                        })
        return handoff_id

    def finish_handoff(self, handoff_id: str, *, status: str, result: dict[str, Any] | None = None) -> None:
        if status not in HANDOFF_STATES - {"requested"}:
            raise ValueError("invalid handoff result state")
        encoded = None if result is None else json.dumps(result, sort_keys=True, allow_nan=False)
        now = _now()
        with self.conn:
            row = self.conn.execute(
                "SELECT mission_id,to_specialist FROM mission_handoffs WHERE handoff_id=?",
                (handoff_id,),
            ).fetchone()
            if row is None:
                raise KeyError("handoff does not exist")
            self.conn.execute(
                "UPDATE mission_handoffs SET status=?,result_json=?,updated_at=? WHERE handoff_id=?",
                (status, encoded, now, handoff_id),
            )
            if status in {"accepted", "running"}:
                self.conn.execute(
                    "UPDATE missions SET status='running',updated_at=? WHERE mission_id=? "
                    "AND status='waiting'", (now, row["mission_id"]),
                )
            self._event(row["mission_id"], row["to_specialist"], "handoff_" + status, status,
                        "Persisted specialist handoff completed", {"handoff_id": handoff_id})

    def bind_run(self, trial_id: str, run_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE missions SET run_id=?,updated_at=? WHERE trial_id=?",
                              (run_id, _now(), trial_id))

    def bind_session(self, mission_id: str, session_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE missions SET session_id=?,updated_at=? WHERE mission_id=?",
                              (session_id, _now(), mission_id))

    def _event(self, mission_id: str, actor: str, event_type: str, status: str, detail: str,
               payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO mission_events(mission_id,created_at,actor,event_type,status,detail,payload_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (mission_id, _now(), actor, event_type, status, detail[:1000],
             json.dumps(payload, sort_keys=True, allow_nan=False)),
        )

    def close(self) -> None:
        self.conn.close()
