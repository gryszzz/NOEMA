from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ResearchTask:
    task_id: str
    market_id: str
    request: str
    priority: float
    status: str
    created_at: str


class ResearchQueueStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_queue (
                task_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                request TEXT NOT NULL,
                priority REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def task_id(market_id: str, request: str) -> str:
        payload = f"{market_id}\n{request.strip()}".encode()
        return hashlib.sha256(payload).hexdigest()[:24]

    def enqueue(
        self,
        *,
        market_id: str,
        request: str,
        priority: float,
    ) -> str:
        clean = request.strip()
        if not clean:
            raise ValueError("research request cannot be empty")
        task_id = self.task_id(market_id, clean)
        self.conn.execute(
            """
            INSERT INTO research_queue
            (task_id, market_id, request, priority, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(task_id) DO UPDATE SET
                priority = MAX(priority, excluded.priority)
            """,
            (
                task_id,
                market_id,
                clean,
                max(0.0, min(priority, 1.0)),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.conn.commit()
        return task_id

    def pending(self, *, limit: int = 50) -> list[ResearchTask]:
        rows = self.conn.execute(
            """
            SELECT task_id, market_id, request, priority, status, created_at
            FROM research_queue
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [ResearchTask(*row) for row in rows]

    def pending_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM research_queue WHERE status = 'pending'"
        ).fetchone()
        return 0 if row is None else int(row[0])
