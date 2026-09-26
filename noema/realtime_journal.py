from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .provenance import canonical_payload_hash


@dataclass(frozen=True)
class RealtimeEvent:
    channel: str
    ticker: str | None
    sequence: int | None
    event_at: datetime | None
    received_at: datetime
    payload_hash: str


class RealtimeJournal:
    """Append-only raw event journal for replay and timing audits."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                ticker TEXT,
                sequence INTEGER,
                event_at TEXT,
                received_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_realtime_events_ticker_time
            ON realtime_events (ticker, received_at)
            """
        )
        self.conn.commit()

    def append(
        self,
        *,
        channel: str,
        payload: Any,
        ticker: str | None = None,
        sequence: int | None = None,
        event_at: datetime | None = None,
        received_at: datetime | None = None,
    ) -> RealtimeEvent:
        received_at = received_at or datetime.now(UTC)
        if received_at.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        if event_at is not None and event_at.tzinfo is None:
            raise ValueError("event_at must be timezone-aware")

        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = canonical_payload_hash(payload)
        self.conn.execute(
            """
            INSERT INTO realtime_events
            (channel, ticker, sequence, event_at, received_at, payload_hash, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel,
                ticker,
                sequence,
                event_at.astimezone(UTC).isoformat() if event_at else None,
                received_at.astimezone(UTC).isoformat(),
                payload_hash,
                payload_json,
            ),
        )
        self.conn.commit()
        return RealtimeEvent(
            channel=channel,
            ticker=ticker,
            sequence=sequence,
            event_at=event_at.astimezone(UTC) if event_at else None,
            received_at=received_at.astimezone(UTC),
            payload_hash=payload_hash,
        )
