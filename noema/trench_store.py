from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .trench_models import TrenchAssessment


class TrenchResearchStore:
    """Persist candidate/rejection decisions and later counterfactual outcomes."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trench_candidates (
                candidate_id TEXT PRIMARY KEY,
                token_mint TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                reference_price_usd REAL NOT NULL,
                reference_liquidity_usd REAL NOT NULL,
                disposition TEXT NOT NULL,
                survival_risk REAL NOT NULL,
                opportunity_score REAL NOT NULL,
                assessment_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trench_counterfactuals (
                candidate_id TEXT NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                final_return_fraction REAL NOT NULL,
                max_return_fraction REAL NOT NULL,
                max_drawdown_fraction REAL NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (candidate_id, horizon_seconds),
                FOREIGN KEY(candidate_id) REFERENCES trench_candidates(candidate_id)
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def candidate_id(token_mint: str, captured_at: str) -> str:
        payload = f"{token_mint.strip()}\n{captured_at}".encode()
        return hashlib.sha256(payload).hexdigest()[:24]

    def record_candidate(
        self,
        *,
        token_mint: str,
        reference_price_usd: float,
        reference_liquidity_usd: float,
        assessment: TrenchAssessment,
        captured_at: datetime | None = None,
    ) -> str:
        if reference_price_usd <= 0:
            raise ValueError("reference_price_usd must be positive")
        if reference_liquidity_usd < 0:
            raise ValueError("reference_liquidity_usd must be non-negative")
        captured_at = captured_at or datetime.now(UTC)
        if captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        captured = captured_at.astimezone(UTC).isoformat()
        candidate_id = self.candidate_id(token_mint, captured)

        payload = json.dumps(asdict(assessment), sort_keys=True, separators=(",", ":"))
        self.conn.execute(
            """
            INSERT OR IGNORE INTO trench_candidates (
                candidate_id, token_mint, captured_at, reference_price_usd,
                reference_liquidity_usd, disposition, survival_risk,
                opportunity_score, assessment_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                token_mint.strip(),
                captured,
                reference_price_usd,
                reference_liquidity_usd,
                assessment.disposition,
                assessment.survival_risk,
                assessment.opportunity_score,
                payload,
            ),
        )
        self.conn.commit()
        return candidate_id

    def record_counterfactual(
        self,
        *,
        candidate_id: str,
        horizon_seconds: int,
        final_return_fraction: float,
        max_return_fraction: float,
        max_drawdown_fraction: float,
        observed_at: datetime | None = None,
    ) -> None:
        if horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be positive")
        if max_drawdown_fraction < 0:
            raise ValueError("max_drawdown_fraction must be non-negative")
        observed_at = observed_at or datetime.now(UTC)
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        exists = self.conn.execute(
            "SELECT 1 FROM trench_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if exists is None:
            raise KeyError(candidate_id)

        self.conn.execute(
            """
            INSERT OR REPLACE INTO trench_counterfactuals (
                candidate_id, horizon_seconds, final_return_fraction,
                max_return_fraction, max_drawdown_fraction, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                horizon_seconds,
                final_return_fraction,
                max_return_fraction,
                max_drawdown_fraction,
                observed_at.astimezone(UTC).isoformat(),
            ),
        )
        self.conn.commit()

    def rejection_stats(self, *, horizon_seconds: int) -> dict[str, float | int]:
        rows = self.conn.execute(
            """
            SELECT c.disposition, o.final_return_fraction, o.max_return_fraction,
                   o.max_drawdown_fraction
            FROM trench_counterfactuals o
            JOIN trench_candidates c ON c.candidate_id = o.candidate_id
            WHERE o.horizon_seconds = ?
            """,
            (horizon_seconds,),
        ).fetchall()
        rejected = [row for row in rows if row[0] == "quarantine"]
        if not rejected:
            return {
                "rejected": 0,
                "half_drawdown_rate": 0.0,
                "missed_2x_rate": 0.0,
                "mean_final_return": 0.0,
            }
        return {
            "rejected": len(rejected),
            "half_drawdown_rate": sum(row[3] >= 0.50 for row in rejected) / len(rejected),
            "missed_2x_rate": sum(row[2] >= 1.0 for row in rejected) / len(rejected),
            "mean_final_return": sum(row[1] for row in rejected) / len(rejected),
        }
