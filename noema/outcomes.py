from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
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
                raw_json TEXT NOT NULL,
                PRIMARY KEY (venue, market_id)
            )
            """
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
    ) -> None:
        if outcome_yes not in {0, 1}:
            raise ValueError("outcome_yes must be 0 or 1")
        self.conn.execute(
            """
            INSERT INTO outcomes (venue, market_id, outcome_yes, resolved_at, raw_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(venue, market_id) DO UPDATE SET
              outcome_yes=excluded.outcome_yes,
              resolved_at=excluded.resolved_at,
              raw_json=excluded.raw_json
            """,
            (venue, market_id, outcome_yes, resolved_at, json.dumps(raw, sort_keys=True)),
        )
        self.conn.commit()

    def evaluate_ledger(self) -> EvaluationSummary:
        rows = self.conn.execute(
            """
            SELECT f.forecast_json, o.outcome_yes
            FROM forecast_ledger f
            JOIN outcomes o
              ON o.venue = f.venue
             AND o.market_id = f.market_id
            """
        ).fetchall()

        if not rows:
            return EvaluationSummary(0, 0.0, 0.0, 0.0)

        probs_and_outcomes: list[tuple[float, int]] = []
        briers: list[float] = []
        log_losses: list[float] = []

        for forecast_json, outcome_yes in rows:
            forecast = json.loads(forecast_json)
            probability = float(forecast["probability_yes"])
            outcome = int(outcome_yes)
            scored = score_forecast(probability, outcome)
            probs_and_outcomes.append((probability, outcome))
            briers.append(scored.brier)
            log_losses.append(scored.log_loss)

        count = len(rows)
        return EvaluationSummary(
            count=count,
            mean_brier=sum(briers) / count,
            mean_log_loss=sum(log_losses) / count,
            calibration_error=expected_calibration_error(probs_and_outcomes),
        )
