from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .cognition_models import CognitionPacket


class CognitionStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cognition_packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                market_id TEXT NOT NULL,
                deployment TEXT NOT NULL,
                response_id TEXT,
                packet_json TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    def append(
        self,
        *,
        deployment: str,
        packet: CognitionPacket,
        response_id: str | None,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO cognition_packets
            (created_at, market_id, deployment, response_id, packet_json,
             input_tokens, output_tokens, total_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                packet.market_id,
                deployment,
                response_id,
                json.dumps(asdict(packet), sort_keys=True),
                input_tokens,
                output_tokens,
                total_tokens,
            ),
        )
        self.conn.commit()

    def calls_since(self, since: datetime) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM cognition_packets
            WHERE created_at >= ?
            """,
            (since.astimezone(UTC).isoformat(),),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def calls_last_hour(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return self.calls_since(now - timedelta(hours=1))

    def tokens_since(self, since: datetime) -> int:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0)
            FROM cognition_packets
            WHERE created_at >= ?
            """,
            (since.astimezone(UTC).isoformat(),),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def tokens_last_hour(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return self.tokens_since(now - timedelta(hours=1))

    def seconds_since_market_call(
        self,
        market_id: str,
        *,
        now: datetime | None = None,
    ) -> float | None:
        row = self.conn.execute(
            """
            SELECT created_at
            FROM cognition_packets
            WHERE market_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()
        if row is None:
            return None
        now = now or datetime.now(UTC)
        created = datetime.fromisoformat(row[0])
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return max(0.0, (now - created.astimezone(UTC)).total_seconds())

    def latest(self) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT created_at, market_id, deployment, packet_json,
                   input_tokens, output_tokens, total_tokens
            FROM cognition_packets
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "created_at": row[0],
            "market_id": row[1],
            "deployment": row[2],
            "packet": json.loads(row[3]),
            "input_tokens": row[4],
            "output_tokens": row[5],
            "total_tokens": row[6],
        }
