"""Small host-side admission gate for heavyweight NOEMA capabilities.

Locks are advisory process locks: they disappear automatically when a process exits,
so a crash cannot leave a worker slot permanently reserved.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Self

_RESOURCE_KINDS = {"docker_worker", "browser", "local_model", "experiment"}


def _lock_dir(*, create: bool = True) -> Path | None:
    path = Path(os.getenv("NOEMA_RESOURCE_LOCK_DIR", "data/runtime-locks"))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        return None
    return path


def memory_snapshot() -> dict:
    """Return an estimated physical-memory availability without collecting payloads."""
    if os.uname().sysname == "Darwin":
        try:
            result = subprocess.run(
                ["memory_pressure", "-Q"], capture_output=True, text=True, timeout=2,
                check=True,
            )
            match = re.search(r"free percentage:\s*(\d+)%", result.stdout)
            if match:
                percent = int(match.group(1))
                return {"available_percent": percent, "source": "memory_pressure"}
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            values[name] = int(value.strip().split()[0])
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        return {"available_percent": max(0, min(100, int(available * 100 / total))),
                "source": "proc_meminfo"}
    except (OSError, KeyError, ValueError, IndexError):
        return {"available_percent": None, "source": "unavailable"}


def memory_admission_reason() -> str | None:
    snapshot = memory_snapshot()
    floor = max(10, min(80, int(os.getenv("NOEMA_MIN_AVAILABLE_MEMORY_PERCENT", "20"))))
    available = snapshot["available_percent"]
    if available is None:
        return "RESOURCE LIMITED: memory pressure is unavailable"
    if available < floor:
        return f"RESOURCE LIMITED: available memory is below {floor}%"
    return None


@dataclass
class ResourceLease:
    kind: str
    _file: object

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def try_acquire(kind: str) -> tuple[ResourceLease | None, str | None]:
    """Admit at most one task per heavyweight class and refuse under memory pressure."""
    if kind not in _RESOURCE_KINDS:
        raise ValueError("unknown resource class")
    reason = memory_admission_reason()
    if reason:
        return None, reason
    # One global heavyweight slot prevents a model load, browser, OpenClaw worker,
    # and experiment from competing for scarce unified memory at the same time.
    lock_path = _lock_dir() / "heavy-workload.lock"
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        active = handle.read(40).strip()
        handle.close()
        label = active.replace("_", " ") if active else "another heavyweight"
        return None, f"QUEUED: {label} workload occupies the single heavyweight slot"
    handle.seek(0)
    handle.truncate()
    handle.write(kind)
    handle.flush()
    return ResourceLease(kind, handle), None


def resource_status() -> dict:
    """Secret-free workstation view of memory pressure and resource slots."""
    snapshot = memory_snapshot()
    floor = max(10, min(80, int(os.getenv("NOEMA_MIN_AVAILABLE_MEMORY_PERCENT", "20"))))
    available = snapshot["available_percent"]
    limited = available is None or available < floor
    lock_dir = _lock_dir(create=False)
    lock_path = None if lock_dir is None else lock_dir / "heavy-workload.lock"
    handle = lock_path.open("a+") if lock_path and lock_path.exists() else None
    if handle is None:
        active = None
    else:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BlockingIOError:
            handle.seek(0)
            active = handle.read(40).strip() or "heavy workload"
        finally:
            handle.close()
    slots = {kind: "working" if kind == active else "idle" for kind in _RESOURCE_KINDS}
    return {
        "state": "resource_limited" if limited else "ready",
        "available_memory_percent": available,
        "memory_source": snapshot["source"],
        "minimum_available_memory_percent": floor,
        "limits": {"docker_workers": 1, "browser_sessions": 1,
                   "large_local_models": 1, "experiments": 1},
        "slots": slots,
        "active_workload": active,
        "detail": ("RESOURCE LIMITED: memory pressure unavailable" if available is None
                   else "RESOURCE LIMITED" if limited else None),
    }
