from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .economic_models import EconomicSnapshot


class EconomicLedger:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS economic_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS economic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                amount_usd TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def append_snapshot(self, snapshot: EconomicSnapshot) -> None:
        payload = {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(snapshot).items()
        }
        self.conn.execute(
            """
            INSERT INTO economic_snapshots (created_at, snapshot_json)
            VALUES (?, ?)
            """,
            (datetime.now(UTC).isoformat(), json.dumps(payload, sort_keys=True)),
        )
        self.conn.commit()

    def append_event(
        self,
        event_type: str,
        *,
        amount_usd: Decimal | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO economic_events
            (created_at, event_type, amount_usd, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                event_type,
                None if amount_usd is None else str(amount_usd),
                json.dumps(payload or {}, sort_keys=True, default=str),
            ),
        )
        self.conn.commit()

    def latest_snapshot(self) -> EconomicSnapshot | None:
        row = self.conn.execute(
            """
            SELECT snapshot_json
            FROM economic_snapshots
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        raw = json.loads(row[0])
        return EconomicSnapshot(
            **{key: Decimal(str(value)) for key, value in raw.items()}
        )
