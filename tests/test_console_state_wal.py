import sqlite3

from noema.dashboard_app import _enable_console_state_wal


def test_console_state_uses_wal_for_persistent_snapshot_merges(tmp_path):
    path = str(tmp_path / "console-state.db")
    _enable_console_state_wal(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
