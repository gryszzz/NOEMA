"""Disk-safety retention for append-only evidence tables (paper only; never touches trades, ledgers or treasury).

The console-state sidecar (``noema.console-state.db``) grew ~10 GB/day because every agent cycle appends full
JSON observation/assessment payloads. Rows older than the retention window are deleted in small id-ordered
batches (short write locks, busy timeout) so freed pages get reused and the file stops growing.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

# table -> timestamp column. Only append-only evidence snapshots; never ledgers, orders, balances or deposits.
EVIDENCE_TABLES: dict[str, str] = {
    "contract_observations": "observed_at",
    "canonical_pair_observations": "observed_at",
    "identity_assessments": "observed_at",
    "settlement_assessments": "observed_at",
    "canonical_quote_observations": "observed_at",
    "economic_comparability_assessments": "observed_at",
}
RESEARCH_TABLES: dict[str, str] = {"autonomous_research_runs": "created_at"}


def _cutoff(hours: float, now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) - timedelta(hours=hours)).isoformat()


def _has(c: sqlite3.Connection, t: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def prune_table(c: sqlite3.Connection, table: str, col: str, cutoff: str, *, batch: int = 500,
                pause_s: float = 0.05, extra_where: str = "") -> int:
    """Delete rows with ``col < cutoff`` in id-ordered batches; returns rows deleted. Ids are monotonic in time."""
    if not _has(c, table):
        return 0
    row = c.execute(f"SELECT MAX(id) FROM {table} WHERE {col} < ?{extra_where}", (cutoff,)).fetchone()
    max_id = row[0] if row else None
    if max_id is None:
        return 0
    total = 0
    while True:
        cur = c.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE id <= ? AND {col} < ?"
                        f"{extra_where} ORDER BY id LIMIT ?)", (max_id, cutoff, batch))
        c.commit()
        total += cur.rowcount
        if cur.rowcount < batch:
            return total
        time.sleep(pause_s)


def prune_console_state(path: str, *, evidence_hours: float = 24.0, research_days: float = 7.0,
                        now: datetime | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"path": os.path.basename(path), "deleted": {}}
    if not os.path.isfile(path):
        out["error"] = "missing"
        return out
    c = sqlite3.connect(path, timeout=60)
    try:
        c.execute("PRAGMA busy_timeout=60000")
        ev_cut = _cutoff(evidence_hours, now)
        for t, col in EVIDENCE_TABLES.items():
            out["deleted"][t] = prune_table(c, t, col, ev_cut)
        rs_cut = _cutoff(research_days * 24, now)
        for t, col in RESEARCH_TABLES.items():
            out["deleted"][t] = prune_table(c, t, col, rs_cut, extra_where=" AND status != 'running'")
        c.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        c.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.getenv("NOEMA_CONSOLE_STATE_DB_PATH", "data/noema.console-state.db"))
    ap.add_argument("--evidence-hours", type=float, default=float(os.getenv("NOEMA_EVIDENCE_RETENTION_HOURS", "24")))
    ap.add_argument("--research-days", type=float, default=float(os.getenv("NOEMA_RESEARCH_RETENTION_DAYS", "7")))
    a = ap.parse_args(argv)
    print(json.dumps(prune_console_state(a.db, evidence_hours=a.evidence_hours, research_days=a.research_days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
