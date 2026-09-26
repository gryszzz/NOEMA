from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .specialist_evolution import EvolutionDecision, SpecialistEvidence


@dataclass(frozen=True)
class EvolutionState:
    specialist: str
    success_streak: int
    failure_streak: int
    review_count: int
    last_reason: str | None
    updated_at: str


class EvolutionStore:
    """Persistence for specialist review streaks and immutable evolution history."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS specialist_evolution_state (
                specialist TEXT PRIMARY KEY,
                success_streak INTEGER NOT NULL,
                failure_streak INTEGER NOT NULL,
                review_count INTEGER NOT NULL,
                last_reason TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS specialist_evolution_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                specialist TEXT NOT NULL,
                created_at TEXT NOT NULL,
                previous_state TEXT NOT NULL,
                next_state TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                decision_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def state(self, specialist: str) -> EvolutionState:
        clean = specialist.strip()
        if not clean:
            raise ValueError("specialist is required")
        row = self.conn.execute(
            """
            SELECT specialist, success_streak, failure_streak, review_count,
                   last_reason, updated_at
            FROM specialist_evolution_state
            WHERE specialist = ?
            """,
            (clean,),
        ).fetchone()
        if row is None:
            return EvolutionState(
                specialist=clean,
                success_streak=0,
                failure_streak=0,
                review_count=0,
                last_reason=None,
                updated_at=datetime.now(UTC).isoformat(),
            )
        return EvolutionState(
            specialist=str(row[0]),
            success_streak=int(row[1]),
            failure_streak=int(row[2]),
            review_count=int(row[3]),
            last_reason=None if row[4] is None else str(row[4]),
            updated_at=str(row[5]),
        )

    def record_review(
        self,
        *,
        specialist: str,
        evidence: SpecialistEvidence,
        decision: EvolutionDecision,
    ) -> int:
        clean = specialist.strip()
        if not clean:
            raise ValueError("specialist is required")

        created_at = datetime.now(UTC).isoformat()
        evidence_json = json.dumps(asdict(evidence), sort_keys=True)
        decision_json = json.dumps(asdict(decision), sort_keys=True, default=str)
        reason = "; ".join(decision.reasons) if decision.reasons else None

        cursor = self.conn.execute(
            """
            INSERT INTO specialist_evolution_reviews (
                specialist, created_at, previous_state, next_state,
                evidence_json, decision_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                clean,
                created_at,
                decision.previous_state.value,
                decision.next_state.value,
                evidence_json,
                decision_json,
            ),
        )
        current = self.state(clean)
        self.conn.execute(
            """
            INSERT INTO specialist_evolution_state (
                specialist, success_streak, failure_streak, review_count,
                last_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(specialist) DO UPDATE SET
                success_streak = excluded.success_streak,
                failure_streak = excluded.failure_streak,
                review_count = excluded.review_count,
                last_reason = excluded.last_reason,
                updated_at = excluded.updated_at
            """,
            (
                clean,
                decision.success_streak,
                decision.failure_streak,
                current.review_count + 1,
                reason,
                created_at,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def latest_review(self, specialist: str) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT created_at, previous_state, next_state, evidence_json, decision_json
            FROM specialist_evolution_reviews
            WHERE specialist = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (specialist.strip(),),
        ).fetchone()
        if row is None:
            return None
        return {
            "created_at": str(row[0]),
            "previous_state": str(row[1]),
            "next_state": str(row[2]),
            "evidence": json.loads(str(row[3])),
            "decision": json.loads(str(row[4])),
        }
