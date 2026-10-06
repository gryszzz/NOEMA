"""Evidence-preserving storage recovery for hosted NOEMA services."""

from __future__ import annotations

import shutil
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

_DEFAULT_MIN_FREE_BYTES = 32 * 1024 * 1024
_STALE_TEMP_AGE_SECONDS = 15 * 60


@dataclass(frozen=True)
class StorageRecoveryReport:
    role: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    minimum_free_bytes: int
    database_bytes: int
    wal_bytes: int
    shm_bytes: int
    reclaimed_bytes: int
    safe_to_write: bool
    actions: tuple[str, ...]
    errors: tuple[str, ...]

    def safe_fields(self) -> dict[str, object]:
        """Return secret/path-free fields suitable for hosted logs and health surfaces."""
        return asdict(self)


def _regular_file_size(path: Path) -> int:
    try:
        metadata = path.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return 0
    if not stat.S_ISREG(metadata.st_mode):
        return 0
    return int(metadata.st_size)


def _checkpoint(path: Path, actions: list[str], errors: list[str]) -> None:
    if not path.is_file():
        return
    try:
        with sqlite3.connect(path, timeout=5) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            if mode and str(mode[0]).lower() == "wal":
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                actions.append("sqlite_wal_checkpoint")
    except sqlite3.Error as exc:
        errors.append(type(exc).__name__)


def _remove_stale_console_temps(directory: Path, actions: list[str], errors: list[str]) -> None:
    """Remove only abandoned snapshot staging files; never canonical databases/evidence."""
    cutoff = time.time() - _STALE_TEMP_AGE_SECONDS
    try:
        entries: Iterable[Path] = directory.iterdir()
    except OSError as exc:
        errors.append(type(exc).__name__)
        return
    for path in entries:
        name = path.name
        if not name.startswith("tmp"):
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except (FileNotFoundError, OSError):
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mtime > cutoff
            or path.suffix
        ):
            continue
        try:
            path.unlink()
            actions.append("removed_abandoned_snapshot_temp")
        except OSError as exc:
            errors.append(type(exc).__name__)


def recover_storage(
    db_path: str,
    *,
    role: str,
    additional_sqlite_paths: Iterable[str] = (),
    minimum_free_bytes: int | None = None,
) -> StorageRecoveryReport:
    """Recover only mechanically safe space and report whether writes may resume.

    Recovery is intentionally conservative:
    - SQLite WAL files are checkpointed/truncated through SQLite itself.
    - On the console replica volume only, abandoned extensionless tmp snapshot
      staging files older than 15 minutes are removed.
    - Primary databases, console-state databases, metadata, research rows and
      evidence files are never deleted, truncated, vacuumed or rewritten here.
    """
    if role not in {"worker", "console"}:
        raise ValueError("role must be worker or console")
    database = Path(db_path).resolve()
    directory = database.parent
    minimum = (
        _DEFAULT_MIN_FREE_BYTES
        if minimum_free_bytes is None
        else max(0, int(minimum_free_bytes))
    )
    actions: list[str] = []
    errors: list[str] = []

    try:
        before = shutil.disk_usage(directory)
    except OSError:
        before = shutil.disk_usage(Path.cwd())

    paths = [database, *(Path(item).resolve() for item in additional_sqlite_paths)]
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        _checkpoint(path, actions, errors)

    if role == "console":
        _remove_stale_console_temps(directory, actions, errors)

    try:
        after = shutil.disk_usage(directory)
    except OSError:
        after = before

    database_bytes = _regular_file_size(database)
    wal_bytes = sum(_regular_file_size(Path(f"{path}-wal")) for path in seen)
    shm_bytes = sum(_regular_file_size(Path(f"{path}-shm")) for path in seen)
    reclaimed = max(0, int(after.free - before.free))
    return StorageRecoveryReport(
        role=role,
        total_bytes=int(after.total),
        used_bytes=int(after.used),
        free_bytes=int(after.free),
        minimum_free_bytes=minimum,
        database_bytes=database_bytes,
        wal_bytes=wal_bytes,
        shm_bytes=shm_bytes,
        reclaimed_bytes=reclaimed,
        safe_to_write=int(after.free) >= minimum,
        actions=tuple(actions),
        errors=tuple(errors),
    )