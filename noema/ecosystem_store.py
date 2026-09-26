from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .ecosystem import EcosystemPlan
from .specialists import SpecialistProfile, SpecialistState


class EcosystemStore:
    """Persistent specialist registry and research-attention audit trail."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ecosystem_specialists (
                name TEXT PRIMARY KEY,
                family TEXT NOT NULL,
                state TEXT NOT NULL,
                resolved INTEGER NOT NULL,
                reliability REAL NOT NULL,
                calibration_error REAL,
                after_cost_return REAL,
                drawdown_fraction REAL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ecosystem_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                dominant_specialist TEXT,
                idle_fraction REAL NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def ensure_specialist(
        self,
        *,
        name: str,
        family: str,
        state: SpecialistState = SpecialistState.SHADOW,
    ) -> None:
        clean_name = name.strip()
        clean_family = family.strip()
        if not clean_name or not clean_family:
            raise ValueError("specialist name and family are required")
        self.conn.execute(
            """
            INSERT OR IGNORE INTO ecosystem_specialists (
                name, family, state, resolved, reliability, calibration_error,
                after_cost_return, drawdown_fraction, updated_at
            ) VALUES (?, ?, ?, 0, 0.0, NULL, NULL, NULL, ?)
            """,
            (
                clean_name,
                clean_family,
                state.value,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def upsert_profile(self, profile: SpecialistProfile) -> None:
        if not profile.name.strip() or not profile.family.strip():
            raise ValueError("specialist name and family are required")
        self.conn.execute(
            """
            INSERT INTO ecosystem_specialists (
                name, family, state, resolved, reliability, calibration_error,
                after_cost_return, drawdown_fraction, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                family = excluded.family,
                state = excluded.state,
                resolved = excluded.resolved,
                reliability = excluded.reliability,
                calibration_error = excluded.calibration_error,
                after_cost_return = excluded.after_cost_return,
                drawdown_fraction = excluded.drawdown_fraction,
                updated_at = excluded.updated_at
            """,
            (
                profile.name,
                profile.family,
                profile.state.value,
                profile.resolved,
                profile.reliability,
                profile.calibration_error,
                profile.after_cost_return,
                profile.drawdown_fraction,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def profiles(self) -> list[SpecialistProfile]:
        rows = self.conn.execute(
            """
            SELECT name, family, state, resolved, reliability, calibration_error,
                   after_cost_return, drawdown_fraction
            FROM ecosystem_specialists
            ORDER BY name
            """
        ).fetchall()
        return [
            SpecialistProfile(
                name=str(name),
                family=str(family),
                state=SpecialistState(str(state)),
                resolved=int(resolved),
                reliability=float(reliability),
                calibration_error=(
                    None if calibration_error is None else float(calibration_error)
                ),
                after_cost_return=(
                    None if after_cost_return is None else float(after_cost_return)
                ),
                drawdown_fraction=(
                    None if drawdown_fraction is None else float(drawdown_fraction)
                ),
            )
            for (
                name,
                family,
                state,
                resolved,
                reliability,
                calibration_error,
                after_cost_return,
                drawdown_fraction,
            ) in rows
        ]

    def record_plan(self, plan: EcosystemPlan) -> int:
        payload = json.dumps(asdict(plan), sort_keys=True, default=str)
        previous = self.conn.execute(
            """
            SELECT id, payload_json
            FROM ecosystem_reviews
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if previous is not None and str(previous[1]) == payload:
            return int(previous[0])

        cursor = self.conn.execute(
            """
            INSERT INTO ecosystem_reviews (
                created_at, dominant_specialist, idle_fraction, payload_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                plan.dominant_specialist,
                plan.idle_fraction,
                payload,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def latest_plan(self) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT payload_json
            FROM ecosystem_reviews
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return json.loads(str(row[0]))
