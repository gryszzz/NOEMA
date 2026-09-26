from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResearchTrial:
    trial_id: str
    family: str
    hypothesis: str
    params_json: str
    feature_set_version: str
    status: str
    created_at: str
    parent_trial_id: str | None


class ResearchTrialStore:
    """Append-oriented registry so failed parameter searches cannot disappear."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_trials (
                trial_id TEXT PRIMARY KEY,
                family TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                params_json TEXT NOT NULL,
                feature_set_version TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                parent_trial_id TEXT
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def canonical_params(params: dict[str, Any]) -> str:
        return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def make_trial_id(
        cls,
        *,
        family: str,
        hypothesis: str,
        params: dict[str, Any],
        feature_set_version: str,
    ) -> str:
        payload = "\n".join(
            (
                family.strip(),
                hypothesis.strip(),
                cls.canonical_params(params),
                feature_set_version.strip(),
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def register(
        self,
        *,
        family: str,
        hypothesis: str,
        params: dict[str, Any],
        feature_set_version: str,
        parent_trial_id: str | None = None,
    ) -> str:
        family = family.strip()
        hypothesis = hypothesis.strip()
        feature_set_version = feature_set_version.strip()
        if not family or not hypothesis or not feature_set_version:
            raise ValueError("family, hypothesis, and feature_set_version are required")

        params_json = self.canonical_params(params)
        trial_id = self.make_trial_id(
            family=family,
            hypothesis=hypothesis,
            params=params,
            feature_set_version=feature_set_version,
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO research_trials (
                trial_id, family, hypothesis, params_json, feature_set_version,
                status, created_at, parent_trial_id
            ) VALUES (?, ?, ?, ?, ?, 'registered', ?, ?)
            """,
            (
                trial_id,
                family,
                hypothesis,
                params_json,
                feature_set_version,
                datetime.now(UTC).isoformat(),
                parent_trial_id,
            ),
        )
        self.conn.commit()
        return trial_id

    def set_status(self, trial_id: str, status: str) -> None:
        clean = status.strip()
        if clean not in {"registered", "running", "rejected", "promoted", "retired"}:
            raise ValueError("invalid research trial status")
        cursor = self.conn.execute(
            "UPDATE research_trials SET status = ? WHERE trial_id = ?",
            (clean, trial_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(trial_id)
        self.conn.commit()

    def count_family(self, family: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM research_trials WHERE family = ?",
            (family,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def recent(
        self,
        *,
        status: str | None = None,
        family: str | None = None,
        limit: int = 50,
    ) -> list[ResearchTrial]:
        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if family is not None:
            clauses.append("family = ?")
            params.append(family)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT trial_id, family, hypothesis, params_json, feature_set_version,
                   status, created_at, parent_trial_id
            FROM research_trials
            {where}
            ORDER BY created_at DESC, trial_id
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [ResearchTrial(*row) for row in rows]

    def get(self, trial_id: str) -> ResearchTrial | None:
        row = self.conn.execute(
            """
            SELECT trial_id, family, hypothesis, params_json, feature_set_version,
                   status, created_at, parent_trial_id
            FROM research_trials
            WHERE trial_id = ?
            """,
            (trial_id,),
        ).fetchone()
        return None if row is None else ResearchTrial(*row)
