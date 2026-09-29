from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_pair_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                canonical_event_id TEXT NOT NULL,
                canonical_proposition_id TEXT NOT NULL,
                semantic_status TEXT NOT NULL,
                settlement_equivalence TEXT NOT NULL,
                observation_hash TEXT NOT NULL UNIQUE,
                observation_json TEXT NOT NULL
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

    def append_canonical_pair_observation(self, observation: dict[str, Any]) -> bool:
        """Persist one real cross-venue identity/quote observation.

        The record remains descriptive. This ledger has no order or execution
        authority and does not promote settlement equivalence.
        """
        identity = observation.get("canonical_identity")
        if not isinstance(identity, dict):
            raise TypeError("canonical identity evidence is required")
        comparison = identity.get("comparison")
        contracts = identity.get("contracts")
        if not isinstance(comparison, dict) or not isinstance(contracts, list) or len(contracts) != 2:
            raise ValueError("a two-venue canonical comparison is required")
        event_id = comparison.get("canonical_event_id")
        proposition_id = comparison.get("canonical_proposition_id")
        semantic_status = comparison.get("semantic_match")
        settlement = comparison.get("settlement_equivalence")
        if not all(isinstance(value, str) and value for value in
                   (event_id, proposition_id, semantic_status, settlement)):
            raise ValueError("canonical comparison fields are incomplete")
        observed_at = str(observation.get("observed_at") or datetime.now(UTC).isoformat())
        payload = json.dumps(observation, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO canonical_pair_observations
            (observed_at, canonical_event_id, canonical_proposition_id,
             semantic_status, settlement_equivalence, observation_hash, observation_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (observed_at, event_id, proposition_id, semantic_status, settlement, digest, payload),
        )
        self.conn.commit()
        return cursor.rowcount == 1

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
