from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import Action, Forecast, MarketSnapshot, Opportunity


class ForecastLedger:
    """Append-only local audit ledger."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forecast_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                venue TEXT NOT NULL,
                market_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                forecast_json TEXT NOT NULL,
                opportunity_json TEXT NOT NULL,
                action_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def append(
        self,
        snapshot: MarketSnapshot,
        forecast: Forecast,
        opportunity: Opportunity,
        action: Action,
    ) -> None:
        def encode(obj: object) -> str:
            return json.dumps(asdict(obj), default=str, sort_keys=True)

        self.conn.execute(
            """
            INSERT INTO forecast_ledger
            (venue, market_id, snapshot_json, forecast_json, opportunity_json, action_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.venue,
                snapshot.market_id,
                encode(snapshot),
                encode(forecast),
                encode(opportunity),
                encode(action),
            ),
        )
        self.conn.commit()

    def has_model_forecast(self, venue: str, market_id: str, model_version: str) -> bool:
        return self.conn.execute(
            """
            SELECT 1 FROM forecast_ledger
            WHERE venue = ? AND market_id = ?
              AND json_extract(forecast_json, '$.model_version') = ?
            LIMIT 1
            """,
            (venue, market_id, model_version),
        ).fetchone() is not None
