from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import MarketSnapshot
from .validation import ValidationResult, validate_market_snapshot


@dataclass(frozen=True)
class SoakStats:
    snapshots: int
    valid_snapshots: int
    invalid_snapshots: int
    distinct_markets: int
    first_captured_at: str | None
    last_captured_at: str | None


class SoakStore:
    """Append-only market-data store for demo/paper research."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT NOT NULL,
                market_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                valid INTEGER NOT NULL CHECK (valid IN (0, 1)),
                validation_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_time
            ON market_snapshots (venue, market_id, captured_at)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS soak_heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
                detail TEXT
            )
            """
        )
        self.conn.commit()

    def append_market(
        self,
        snapshot: MarketSnapshot,
        validation: ValidationResult | None = None,
    ) -> ValidationResult:
        validation = validation or validate_market_snapshot(snapshot)
        self.conn.execute(
            """
            INSERT INTO market_snapshots
            (venue, market_id, captured_at, valid, validation_json, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.venue,
                snapshot.market_id,
                snapshot.captured_at.astimezone(UTC).isoformat(),
                int(validation.valid),
                json.dumps(asdict(validation), sort_keys=True),
                json.dumps(asdict(snapshot), default=str, sort_keys=True),
            ),
        )
        self.conn.commit()
        return validation

    def heartbeat(self, source: str, *, ok: bool, detail: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO soak_heartbeats (created_at, source, ok, detail)
            VALUES (?, ?, ?, ?)
            """,
            (datetime.now(UTC).isoformat(), source, int(ok), detail),
        )
        self.conn.commit()

    def stats(self) -> SoakStats:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(valid),
                COUNT(*) - SUM(valid),
                COUNT(DISTINCT venue || ':' || market_id),
                MIN(captured_at),
                MAX(captured_at)
            FROM market_snapshots
            """
        ).fetchone()
        snapshots = int(row[0] or 0)
        return SoakStats(
            snapshots=snapshots,
            valid_snapshots=int(row[1] or 0),
            invalid_snapshots=int(row[2] or 0),
            distinct_markets=int(row[3] or 0),
            first_captured_at=row[4],
            last_captured_at=row[5],
        )

    def latest_market_rows(self, limit: int = 100) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT venue, market_id, captured_at, valid, validation_json, snapshot_json
            FROM market_snapshots
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "venue": row[0],
                "market_id": row[1],
                "captured_at": row[2],
                "valid": bool(row[3]),
                "validation": json.loads(row[4]),
                "snapshot": json.loads(row[5]),
            }
            for row in rows
        ]
