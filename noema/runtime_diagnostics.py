"""Process-bound runtime liveness evidence.

A recent database heartbeat is useful freshness evidence, but cannot by itself
prove that the process which wrote it is still running. Store a process identity
with each agent status and verify that identity before reporting LIVE.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeProcessIdentity:
    pid: int
    started_at: str
    checkout_path: str
    commit: str | None
    database_path: str
    working_tree_dirty: bool | None = None


ProcessProbe = Callable[[RuntimeProcessIdentity, datetime], str]


def capture_process_identity(database_path: str) -> RuntimeProcessIdentity:
    checkout = Path(__file__).resolve().parent.parent
    started_at = _observed_process_start(os.getpid()) or datetime.now(UTC)
    revision: str | None = None
    working_tree_dirty: bool | None = None
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=2,
        )
        revision = result.stdout.strip() or None
        dirty = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True, capture_output=True, text=True, timeout=2,
        )
        working_tree_dirty = bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return RuntimeProcessIdentity(
        pid=os.getpid(),
        started_at=started_at.isoformat(),
        checkout_path=str(checkout),
        commit=revision,
        database_path=str(Path(database_path).resolve()),
        working_tree_dirty=working_tree_dirty,
    )


def _observed_process_start(pid: int) -> datetime | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=True, capture_output=True, text=True, timeout=2,
        )
        return _parse_ps_start(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def identity_from_mapping(value: object) -> RuntimeProcessIdentity | None:
    if not isinstance(value, dict):
        return None
    try:
        identity = RuntimeProcessIdentity(
            pid=int(value["pid"]),
            started_at=str(value["started_at"]),
            checkout_path=str(value["checkout_path"]),
            commit=None if value.get("commit") is None else str(value["commit"]),
            database_path=str(value["database_path"]),
            working_tree_dirty=(
                value.get("working_tree_dirty")
                if isinstance(value.get("working_tree_dirty"), bool) else None
            ),
        )
        if identity.pid <= 0 or not identity.checkout_path or not identity.database_path:
            return None
        datetime.fromisoformat(identity.started_at)
        return identity
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def probe_local_process(
    identity: RuntimeProcessIdentity, now: datetime,
) -> str:
    """Return a status only when PID, command, start time, and checkout agree."""
    if identity.pid <= 0:
        return "PID MISMATCH"
    try:
        result = subprocess.run(
            ["ps", "-p", str(identity.pid), "-o", "lstart=", "-o", "command="],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    if result.returncode != 0 or not result.stdout.strip():
        return "PROCESS MISSING"
    try:
        fields = result.stdout.strip().split(None, 5)
        if len(fields) != 6:
            return "UNKNOWN"
        process_start = datetime.strptime(
            " ".join(fields[:5]), "%a %b %d %H:%M:%S %Y",
        ).astimezone(UTC)
        command = fields[5]
    except (ValueError, TypeError):
        return "UNKNOWN"
    if "noema.agent_entry" not in command and "noema-agent" not in command:
        return "PID MISMATCH"

    recorded_start = datetime.fromisoformat(identity.started_at)
    if recorded_start.tzinfo is None:
        recorded_start = recorded_start.replace(tzinfo=UTC)
    if abs((process_start - recorded_start.astimezone(UTC)).total_seconds()) > 5:
        return "PID MISMATCH"

    process_checkout = _process_cwd(identity.pid)
    if process_checkout is None:
        return "UNKNOWN"
    if process_checkout != str(Path(identity.checkout_path).resolve()):
        return "WRONG CHECKOUT"
    database_state = _process_database_state(identity.pid, identity.database_path)
    if database_state != "MATCH":
        return database_state
    return "PROCESS MATCH"


def _parse_ps_start(value: str) -> datetime:
    return datetime.strptime(
        value.strip(), "%a %b %d %H:%M:%S %Y",
    ).astimezone(UTC)


def diagnose_runtime(
    *,
    running: bool,
    heartbeat_age_seconds: float | None,
    identity_value: object,
    database_path: str,
    checkout_path: str | None = None,
    now: datetime | None = None,
    stale_after_seconds: float = 90.0,
    process_probe: ProcessProbe = probe_local_process,
) -> dict[str, Any]:
    """Classify liveness without treating a persisted heartbeat as proof."""
    now = now or datetime.now(UTC)
    expected_checkout = str(Path(checkout_path or Path(__file__).resolve().parent.parent).resolve())
    identity = identity_from_mapping(identity_value)
    state = "UNKNOWN"
    process_state = "UNKNOWN"

    if not running:
        state = "STOPPED"
    elif heartbeat_age_seconds is None or heartbeat_age_seconds < 0:
        state = "UNKNOWN"
    elif identity is None:
        state = "STALE" if heartbeat_age_seconds > stale_after_seconds else "UNKNOWN"
    elif str(Path(identity.database_path).resolve()) != str(Path(database_path).resolve()):
        state = "WRONG DATABASE"
    elif str(Path(identity.checkout_path).resolve()) != expected_checkout:
        state = "WRONG CHECKOUT"
    else:
        process_state = process_probe(identity, now)
        if process_state in {
            "PROCESS MISSING", "PID MISMATCH", "WRONG CHECKOUT", "WRONG DATABASE",
        }:
            state = process_state
        elif process_state != "PROCESS MATCH":
            state = "UNKNOWN"
        elif heartbeat_age_seconds > stale_after_seconds:
            state = "HEARTBEAT STUCK"
        else:
            state = "LIVE"

    return {
        "state": state,
        "process_state": process_state,
        "process_identity": None if identity is None else asdict(identity),
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "heartbeat_fresh": (
            heartbeat_age_seconds is not None
            and 0 <= heartbeat_age_seconds <= stale_after_seconds
        ),
    }


def _process_cwd(pid: int) -> str | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        if proc_cwd.exists():
            return str(proc_cwd.resolve())
    except OSError:
        return None
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return str(Path(line[1:]).resolve())
    return None


def _process_database_state(pid: int, database_path: str) -> str:
    """Verify the process has the configured SQLite file open."""
    expected = str(Path(database_path).resolve())
    proc_fds = Path(f"/proc/{pid}/fd")
    if proc_fds.is_dir():
        try:
            opened = {str(fd.resolve()) for fd in proc_fds.iterdir()}
        except OSError:
            return "UNKNOWN"
        return "MATCH" if expected in opened else "WRONG DATABASE"

    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    opened = {
        str(Path(line[1:]).resolve())
        for line in result.stdout.splitlines()
        if line.startswith("n") and len(line) > 1
    }
    return "MATCH" if expected in opened else "WRONG DATABASE"
