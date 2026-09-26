from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReplaySnapshot:
    venue: str
    market_id: str
    captured_at: str
    valid: bool
    snapshot: dict[str, object]


class SnapshotReplay:
    """Read-only iterator over historical soak snapshots."""

    def __init__(self, path: str = "data/noema.db") -> None:
        if not Path(path).exists():
            raise FileNotFoundError(path)
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def iter_market(
        self,
        venue: str,
        market_id: str,
        *,
        valid_only: bool = True,
    ):
        sql = """
            SELECT venue, market_id, captured_at, valid, snapshot_json
            FROM market_snapshots
            WHERE venue = ? AND market_id = ?
        """
        params: list[object] = [venue, market_id]
        if valid_only:
            sql += " AND valid = 1"
        sql += " ORDER BY captured_at ASC, id ASC"

        for row in self.conn.execute(sql, params):
            yield ReplaySnapshot(
                venue=row[0],
                market_id=row[1],
                captured_at=row[2],
                valid=bool(row[3]),
                snapshot=json.loads(row[4]),
            )
