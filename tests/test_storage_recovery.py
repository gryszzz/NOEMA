from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from noema.storage_recovery import recover_storage


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("INSERT INTO evidence(payload) VALUES (?)", ("keep-me",))
        conn.commit()


def test_recovery_never_deletes_primary_database_or_evidence(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    _db(db)

    report = recover_storage(str(db), role="worker", minimum_free_bytes=0)

    assert db.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT payload FROM evidence").fetchone()[0] == "keep-me"
    assert report.safe_to_write is True


def test_console_recovery_removes_only_old_extensionless_snapshot_temps(tmp_path: Path) -> None:
    db = tmp_path / "noema-console-state.db"
    _db(db)
    stale = tmp_path / "tmpabandoned"
    stale.write_bytes(b"x" * 1024)
    recent = tmp_path / "tmpactive"
    recent.write_bytes(b"y")
    protected = tmp_path / "tmpresearch.json"
    protected.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    os.utime(protected, (old, old))

    report = recover_storage(str(db), role="console", minimum_free_bytes=0)

    assert not stale.exists()
    assert recent.exists()
    assert protected.exists()
    assert "removed_abandoned_snapshot_temp" in report.actions


def test_storage_gate_can_hold_writes_without_mutating_database(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    _db(db)
    original = db.read_bytes()

    report = recover_storage(
        str(db),
        role="worker",
        minimum_free_bytes=10**18,
    )

    assert report.safe_to_write is False
    assert db.read_bytes() == original