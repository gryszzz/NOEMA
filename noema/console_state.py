"""Durable console-owned state kept separate from replaceable worker snapshots."""

from __future__ import annotations

import os
from pathlib import Path


def console_state_db_path(runtime_db_path: str | Path | None = None) -> str:
    """Return the persistent sidecar path for console-collected account state.

    The sidecar lives beside the worker replica by default, so a mounted data
    volume keeps it durable while atomic worker snapshot replacement cannot
    replace the inode that account/history writers use.
    """
    configured = os.getenv("NOEMA_CONSOLE_STATE_DB_PATH")
    if configured:
        return configured
    runtime_path = Path(runtime_db_path or os.getenv("NOEMA_DB_PATH", "data/noema.db"))
    suffix = runtime_path.suffix or ".db"
    return str(runtime_path.with_name(f"{runtime_path.stem}.console-state{suffix}"))
