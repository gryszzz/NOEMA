from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SoakQualityReport:
    snapshots: int
    valid_fraction: float
    heartbeat_success_fraction: float
    heartbeat_count: int


def build_soak_quality_report(path: str = "data/noema.db") -> SoakQualityReport:
    if not Path(path).exists():
        return SoakQualityReport(0, 0.0, 0.0, 0)

    conn = sqlite3.connect(path)
    snapshots, valid = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(valid), 0) FROM market_snapshots"
    ).fetchone()
    heartbeats, heartbeat_ok = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(ok), 0) FROM soak_heartbeats"
    ).fetchone()

    snapshots = int(snapshots or 0)
    heartbeats = int(heartbeats or 0)
    return SoakQualityReport(
        snapshots=snapshots,
        valid_fraction=(float(valid or 0) / snapshots) if snapshots else 0.0,
        heartbeat_success_fraction=(
            float(heartbeat_ok or 0) / heartbeats if heartbeats else 0.0
        ),
        heartbeat_count=heartbeats,
    )
