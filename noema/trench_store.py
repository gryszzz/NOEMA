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
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS trench_candidate_maturities (
                candidate_id TEXT PRIMARY KEY,
                target_at TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                next_retry_at TEXT,
                detail TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES trench_candidates(candidate_id)
            )"""
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
        launch_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trench_launches'"
        ).fetchone()
        launch = (self.conn.execute(
            "SELECT first_pool_at FROM trench_launches WHERE mint=?", (token_mint.strip(),)
        ).fetchone() if launch_table else None)
        if launch:
            from datetime import timedelta
            target_at = datetime.fromisoformat(str(launch[0])).astimezone(UTC) + timedelta(seconds=3600)
            self.conn.execute(
                """INSERT OR IGNORE INTO trench_candidate_maturities
                   (candidate_id,target_at,state,updated_at) VALUES (?,?,?,?)""",
                (candidate_id, target_at.isoformat(), "PENDING", datetime.now(UTC).isoformat()),
            )
        self.conn.commit()
        return candidate_id

    def update_maturity(
        self, candidate_id: str, *, state: str, attempted_at: datetime | None = None,
        next_retry_at: str | None = None, detail: str | None = None,
    ) -> None:
        if state not in {"PENDING", "DUE", "RETRYABLE", "RETRYING", "RECORDED", "MISSED"}:
            raise ValueError("invalid candidate maturity state")
        attempted_raw = attempted_at
        self.conn.execute(
            """UPDATE trench_candidate_maturities SET state=?,
                 attempts=attempts + ?,last_attempt_at=COALESCE(?,last_attempt_at),
                 next_retry_at=?,detail=?,updated_at=? WHERE candidate_id=?""",
            (state, 1 if attempted_raw is not None and state in {"RETRYABLE", "RETRYING", "RECORDED", "MISSED"} else 0,
             None if attempted_raw is None else attempted_raw.astimezone(UTC).isoformat(),
             next_retry_at, detail, datetime.now(UTC).isoformat(), candidate_id),
        )
        self.conn.commit()

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

    def candidate_for_token(
        self,
        token_mint: str,
    ) -> tuple[str, datetime, float] | None:
        row = self.conn.execute(
            """
            SELECT candidate_id, captured_at, reference_price_usd
            FROM trench_candidates
            WHERE token_mint = ?
            ORDER BY captured_at ASC
            LIMIT 1
            """,
            (token_mint.strip(),),
        ).fetchone()
        if row is None:
            return None
        captured = datetime.fromisoformat(str(row[1]))
        if captured.tzinfo is None:
            raise ValueError("stored candidate timestamp must be timezone-aware")
        return str(row[0]), captured.astimezone(UTC), float(row[2])

    def has_counterfactual(
        self,
        candidate_id: str,
        horizon_seconds: int,
    ) -> bool:
        return self.conn.execute(
            """
            SELECT 1
            FROM trench_counterfactuals
            WHERE candidate_id = ? AND horizon_seconds = ?
            """,
            (candidate_id, horizon_seconds),
        ).fetchone() is not None

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
