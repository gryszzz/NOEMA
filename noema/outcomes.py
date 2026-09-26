from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .evaluation import expected_calibration_error, score_forecast


@dataclass(frozen=True)
class EvaluationSummary:
    count: int
    mean_brier: float
    mean_log_loss: float
    calibration_error: float


class OutcomeStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                venue TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_yes INTEGER NOT NULL CHECK (outcome_yes IN (0, 1)),
                resolved_at TEXT,
                first_seen_at TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (venue, market_id)
            )
            """
        )
        if "first_seen_at" not in {
            row[1] for row in self.conn.execute("PRAGMA table_info(outcomes)")
        }:
            # Backfilled legacy rows were not demonstrably available earlier.
            self.conn.execute("ALTER TABLE outcomes ADD COLUMN first_seen_at TEXT")
            self.conn.execute(
                "UPDATE outcomes SET first_seen_at = ? WHERE first_seen_at IS NULL",
                (datetime.now(UTC).isoformat(),),
            )
        self.conn.commit()

    def upsert(
        self,
        *,
        venue: str,
        market_id: str,
        outcome_yes: int,
        resolved_at: str | None,
        raw: dict[str, object],
        seen_at: datetime | None = None,
    ) -> None:
        if outcome_yes not in {0, 1}:
            raise ValueError("outcome_yes must be 0 or 1")
        seen_at = seen_at or datetime.now(UTC)
        if seen_at.tzinfo is None:
            raise ValueError("seen_at must be timezone-aware")
        self.conn.execute(
            """
            INSERT INTO outcomes
            (venue, market_id, outcome_yes, resolved_at, first_seen_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue, market_id) DO UPDATE SET
              outcome_yes=excluded.outcome_yes,
              resolved_at=excluded.resolved_at,
              first_seen_at=CASE WHEN outcomes.outcome_yes != excluded.outcome_yes
                  THEN excluded.first_seen_at ELSE outcomes.first_seen_at END,
              raw_json=excluded.raw_json
            """,
            (
                venue, market_id, outcome_yes, resolved_at,
                seen_at.astimezone(UTC).isoformat(), json.dumps(raw, sort_keys=True),
            ),
        )
        self.conn.commit()

    def evaluate_ledger(self) -> EvaluationSummary:
        try:
            rows = self.conn.execute(
                """
                SELECT f.venue, f.market_id, f.snapshot_json, f.forecast_json,
                       o.outcome_yes, o.resolved_at
                FROM forecast_ledger f
                JOIN outcomes o
                  ON o.venue = f.venue AND o.market_id = f.market_id
                ORDER BY f.id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

        if not rows:
            return EvaluationSummary(0, 0.0, 0.0, 0.0)

        probs_and_outcomes: list[tuple[float, int]] = []
        briers: list[float] = []
        log_losses: list[float] = []

        seen: set[tuple[str, str, str]] = set()
        for venue, ticker, snapshot_json, forecast_json, outcome_yes, resolved_at in rows:
            forecast = json.loads(forecast_json)
            snapshot = json.loads(snapshot_json)
            if not resolved_at:
                continue
            try:
                capture = datetime.fromisoformat(snapshot["captured_at"])
                resolved = datetime.fromisoformat(resolved_at)
            except (KeyError, ValueError):
                continue
            if capture.tzinfo is None or resolved.tzinfo is None or capture >= resolved:
                continue
            key = (venue, ticker, str(forecast.get("model_version", "unknown")))
            if key in seen:
                continue
            seen.add(key)
            probability = float(forecast["probability_yes"])
            outcome = int(outcome_yes)
            scored = score_forecast(probability, outcome)
            probs_and_outcomes.append((probability, outcome))
            briers.append(scored.brier)
            log_losses.append(scored.log_loss)

        count = len(briers)
        if not count:
            return EvaluationSummary(0, 0.0, 0.0, 0.0)
        return EvaluationSummary(
            count=count,
            mean_brier=sum(briers) / count,
            mean_log_loss=sum(log_losses) / count,
            calibration_error=expected_calibration_error(probs_and_outcomes),
        )
