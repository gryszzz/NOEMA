"""Disk-safety retention for append-only evidence tables (paper only; never touches trades, ledgers or treasury).

The console-state sidecar (``noema.console-state.db``) grew ~10 GB/day because every agent cycle appends full
JSON observation/assessment payloads. Rows older than the retention window are deleted in small id-ordered
batches (short write locks, busy timeout) so freed pages get reused and the file stops growing.

``canonical_pair_observations`` (the largest, ~57 KB/row of JSON) is no longer deleted: payloads are written as
NZ1 zlib blobs (``noema.ledger.stored_pair_payload``), rows stay hot for ``NOEMA_PAIR_HOT_HOURS`` (48h), then
move to verified zstd parquet under ``data/cold/noema.console-state/`` (``noema.cold_store``). Paper-fill rows
are never archived: ``mature_paper_pairs`` must keep finding them until their outcome settles.
``noema.db`` ``outcome_observations`` (write-only audit copy of resolved outcomes) is archived the same way.
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
    "identity_assessments": "observed_at",
    "settlement_assessments": "observed_at",
    "canonical_quote_observations": "observed_at",
    "economic_comparability_assessments": "observed_at",
}
RESEARCH_TABLES: dict[str, str] = {"autonomous_research_runs": "created_at"}
# table -> (timestamp column, payload columns to decode, rows never archived). Archived, never deleted outright.
ARCHIVE_TABLES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "canonical_pair_observations": ("observed_at", ("observation_json",), (
        "typeof(observation_json) != 'text' OR instr(observation_json, 'paper_fill_simulated') = 0")),
}
NOEMA_DB_ARCHIVE_TABLES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "outcome_observations": ("observed_at", ("raw_json",), ""),
}
PAIR_HOT_HOURS = float(os.getenv("NOEMA_PAIR_HOT_HOURS", "48"))


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


def archive_tables(c: sqlite3.Connection, path: str, tables: dict[str, tuple[str, tuple[str, ...], str]],
                   hours: float, now: datetime | None = None, root: str | None = None) -> dict[str, Any]:
    """Verified parquet compaction (``noema.cold_store``); a failure leaves rows in SQLite."""
    out: dict[str, Any] = {}
    try:
        from .cold_store import compact_table, default_root
        for t, (col, payload, keep) in tables.items():
            res = compact_table(c, t, ts_col=col, cutoff=_cutoff(hours, now), root=root or default_root(path),
                                payload_cols=payload, batch=2000, where_extra=keep)
            out[t] = res["archived"] if not res["errors"] else res
    except Exception as exc:  # noqa: BLE001 - retention must never take the watchdog down
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return out


def compact_noema_db(path: str, *, hours: float | None = None, now: datetime | None = None) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {"error": "missing"}
    c = sqlite3.connect(path, timeout=60)
    try:
        c.execute("PRAGMA busy_timeout=60000")
        return archive_tables(c, path, NOEMA_DB_ARCHIVE_TABLES, PAIR_HOT_HOURS if hours is None else hours, now)
    finally:
        c.close()


def prune_console_state(path: str, *, evidence_hours: float = 24.0, research_days: float = 7.0,
                        now: datetime | None = None, pair_hot_hours: float | None = None) -> dict[str, Any]:
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
        out["archived"] = archive_tables(c, path, ARCHIVE_TABLES,
                                         PAIR_HOT_HOURS if pair_hot_hours is None else pair_hot_hours, now)
        c.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        c.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.getenv("NOEMA_CONSOLE_STATE_DB_PATH", "data/noema.console-state.db"))
    ap.add_argument("--evidence-hours", type=float, default=float(os.getenv("NOEMA_EVIDENCE_RETENTION_HOURS", "24")))
    ap.add_argument("--research-days", type=float, default=float(os.getenv("NOEMA_RESEARCH_RETENTION_DAYS", "7")))
    ap.add_argument("--noema-db", help="also archive noema.db outcome_observations older than the hot window")
    ap.add_argument("--recompress", action="store_true",
                    help="one-time: rewrite existing TEXT canonical pair payloads as NZ1 blobs (lossless)")
    a = ap.parse_args(argv)
    if a.recompress:
        from .cold_store import recompress_payloads
        c = sqlite3.connect(a.db, timeout=60)
        c.execute("PRAGMA busy_timeout=60000")
        try:
            n = recompress_payloads(c, "canonical_pair_observations", "observation_json",
                                    keep_text_if="paper_fill_simulated")
        finally:
            c.close()
        print(json.dumps({"recompressed": n}))
    print(json.dumps(prune_console_state(a.db, evidence_hours=a.evidence_hours, research_days=a.research_days)))
    if a.noema_db:
        print(json.dumps({"noema_db": compact_noema_db(a.noema_db)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
