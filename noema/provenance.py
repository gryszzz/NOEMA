from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    source: str
    source_type: str
    observed_at: datetime
    retrieved_at: datetime
    payload_hash: str
    payload_json: str


def canonical_payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class EvidenceStore:
    """Append-only provenance store for facts used by forecasting models."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_records (
                evidence_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def append(
        self,
        *,
        evidence_id: str,
        source: str,
        source_type: str,
        observed_at: datetime,
        payload: Any,
        retrieved_at: datetime | None = None,
    ) -> EvidenceRecord:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        retrieved_at = retrieved_at or datetime.now(UTC)
        if retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        record = EvidenceRecord(
            evidence_id=evidence_id,
            source=source,
            source_type=source_type,
            observed_at=observed_at.astimezone(UTC),
            retrieved_at=retrieved_at.astimezone(UTC),
            payload_hash=canonical_payload_hash(payload),
            payload_json=payload_json,
        )
        self.conn.execute(
            """
            INSERT INTO evidence_records
            (evidence_id, source, source_type, observed_at, retrieved_at, payload_hash, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.evidence_id,
                record.source,
                record.source_type,
                record.observed_at.isoformat(),
                record.retrieved_at.isoformat(),
                record.payload_hash,
                record.payload_json,
            ),
        )
        self.conn.commit()
        return record

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        row = self.conn.execute(
            """
            SELECT evidence_id, source, source_type, observed_at, retrieved_at,
                   payload_hash, payload_json
            FROM evidence_records
            WHERE evidence_id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            return None
        return EvidenceRecord(
            evidence_id=row[0],
            source=row[1],
            source_type=row[2],
            observed_at=datetime.fromisoformat(row[3]),
            retrieved_at=datetime.fromisoformat(row[4]),
            payload_hash=row[5],
            payload_json=row[6],
        )

    def verify_integrity(self, evidence_id: str) -> bool:
        record = self.get(evidence_id)
        if record is None:
            return False
        payload = json.loads(record.payload_json)
        return canonical_payload_hash(payload) == record.payload_hash
