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
        # Canonical-market graph projections plus append-only source evidence.
        # These tables contain public market data and identifiers only.
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS canonical_events (
                canonical_event_id TEXT PRIMARY KEY,
                topic_id TEXT,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canonical_propositions (
                canonical_proposition_id TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL,
                proposition_type TEXT,
                outcome_id TEXT,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS venue_contracts (
                venue TEXT NOT NULL,
                contract_id TEXT NOT NULL,
                canonical_event_id TEXT,
                canonical_proposition_id TEXT,
                identity_status TEXT NOT NULL,
                latest_observed_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (venue, contract_id)
            );
            CREATE TABLE IF NOT EXISTS contract_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT NOT NULL,
                contract_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                observation_hash TEXT NOT NULL UNIQUE,
                observation_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identity_assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                canonical_event_id TEXT,
                canonical_proposition_id TEXT,
                status_json TEXT NOT NULL,
                assessment_hash TEXT NOT NULL UNIQUE,
                evidence_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settlement_assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                canonical_proposition_id TEXT,
                status TEXT NOT NULL,
                assessment_hash TEXT NOT NULL UNIQUE,
                evidence_json TEXT NOT NULL
            );
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
        # Materialize the graph from the same immutable observation so Home
        # and later analysis can query either current links or historical proof.
        self._append_canonical_graph(observation, observed_at, payload)
        self.conn.commit()
        return cursor.rowcount == 1

    def _append_canonical_graph(
        self, observation: dict[str, Any], observed_at: str, payload: str,
    ) -> None:
        identity = observation["canonical_identity"]
        comparison = identity["comparison"]
        contracts = identity["contracts"]
        event_id = comparison.get("canonical_event_id")
        proposition_id = comparison.get("canonical_proposition_id")
        settlement = comparison.get("settlement_equivalence", "unverified")
        levels = comparison.get("levels", {})
        assessment = json.dumps(levels, sort_keys=True, separators=(",", ":"))
        assessment_hash = hashlib.sha256(
            f"{observed_at}:{assessment}:{payload}".encode()
        ).hexdigest()
        if isinstance(event_id, str) and event_id:
            first = contracts[0]
            self.conn.execute(
                """INSERT INTO canonical_events
                   (canonical_event_id, topic_id, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(canonical_event_id) DO UPDATE SET
                     topic_id=COALESCE(excluded.topic_id, canonical_events.topic_id),
                     metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                (event_id, first.get("topic_id"), json.dumps({
                    "sport": first.get("sport"), "season": first.get("season"),
                    "event_type": first.get("event_type"),
                }, sort_keys=True), observed_at),
            )
        if isinstance(event_id, str) and event_id and isinstance(proposition_id, str) and proposition_id:
            first = contracts[0]
            self.conn.execute(
                """INSERT INTO canonical_propositions
                   (canonical_proposition_id, canonical_event_id, proposition_type,
                    outcome_id, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(canonical_proposition_id) DO UPDATE SET
                     metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                (proposition_id, event_id, first.get("proposition_type") or first.get("event_type"),
                 first.get("canonical_outcome_id") or first.get("outcome_id"),
                 json.dumps({"identity_status": "identified"}, sort_keys=True), observed_at),
            )
        for contract in contracts:
            venue = contract.get("venue")
            contract_id = contract.get("contract_id")
            if not isinstance(venue, str) or not isinstance(contract_id, str):
                continue
            raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str)
            observation_hash = hashlib.sha256(
                f"{venue}:{contract_id}:{observed_at}:{raw}:{payload}".encode()
            ).hexdigest()
            self.conn.execute(
                """INSERT OR IGNORE INTO contract_observations
                   (venue, contract_id, observed_at, observation_hash, observation_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (venue, contract_id, observed_at, observation_hash, payload),
            )
            self.conn.execute(
                """INSERT INTO venue_contracts
                   (venue, contract_id, canonical_event_id, canonical_proposition_id,
                    identity_status, latest_observed_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(venue, contract_id) DO UPDATE SET
                     canonical_event_id=excluded.canonical_event_id,
                     canonical_proposition_id=excluded.canonical_proposition_id,
                     identity_status=excluded.identity_status,
                     latest_observed_at=excluded.latest_observed_at,
                     metadata_json=excluded.metadata_json""",
                (venue, contract_id, contract.get("canonical_event_id"),
                 contract.get("canonical_proposition_id"),
                 str(contract.get("identity_status") or "unresolved"), observed_at, raw),
            )
        self.conn.execute(
            """INSERT OR IGNORE INTO identity_assessments
               (observed_at, canonical_event_id, canonical_proposition_id,
                status_json, assessment_hash, evidence_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (observed_at, event_id, proposition_id, assessment, assessment_hash, payload),
        )
        settlement_hash = hashlib.sha256(
            f"{observed_at}:{proposition_id}:{settlement}:{payload}".encode()
        ).hexdigest()
        self.conn.execute(
            """INSERT OR IGNORE INTO settlement_assessments
               (observed_at, canonical_proposition_id, status, assessment_hash, evidence_json)
               VALUES (?, ?, ?, ?, ?)""",
            (observed_at, proposition_id, str(settlement), settlement_hash, payload),
        )

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
