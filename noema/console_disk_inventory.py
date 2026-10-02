"""Read-only inventory of top-level files on the operations-console volume."""

from __future__ import annotations

import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Deliberately fixed to the attached console volume. Do not derive this from an
# arbitrary request value or follow a service-configured path elsewhere.
_CONSOLE_VOLUME = Path("/opt/render/project/src/console-data")
_SNAPSHOT_TEMP_MIN_BYTES = 1024 * 1024


def _file_type(name: str, mode: int, size_bytes: int) -> str:
    if stat.S_ISLNK(mode):
        return "symbolic_link"
    if not stat.S_ISREG(mode):
        return "other_file"
    if name.endswith("-wal"):
        return "sqlite_wal"
    if name.endswith("-shm"):
        return "sqlite_shm"
    if name.endswith((".sqlite", ".sqlite3", ".db")):
        return "sqlite_database"
    if name.endswith(".worker.json"):
        return "worker_metadata"
    if name.startswith("tmp") and not Path(name).suffix:
        if size_bytes >= _SNAPSHOT_TEMP_MIN_BYTES:
            return "extensionless_snapshot_temp_candidate"
        return "extensionless_temp_candidate"
    return "other_file"


def build_console_disk_inventory() -> dict[str, Any]:
    """Return names and filesystem metadata only; never open or mutate file data."""
    if _CONSOLE_VOLUME.is_symlink() or not _CONSOLE_VOLUME.is_dir():
        raise OSError("console volume is unavailable")

    usage = shutil.disk_usage(_CONSOLE_VOLUME)
    files: list[dict[str, Any]] = []
    allocated_inodes: set[tuple[int, int]] = set()
    accounted_bytes = 0
    directories_not_scanned = 0

    with os.scandir(_CONSOLE_VOLUME) as entries:
        for entry in entries:
            metadata = entry.stat(follow_symlinks=False)
            mode = metadata.st_mode
            if stat.S_ISDIR(mode):
                directories_not_scanned += 1
                continue

            size_bytes = metadata.st_size
            allocated_blocks = getattr(metadata, "st_blocks", None)
            allocated_bytes = size_bytes if allocated_blocks is None else allocated_blocks * 512
            inode_key = (metadata.st_dev, metadata.st_ino)
            if inode_key not in allocated_inodes:
                allocated_inodes.add(inode_key)
                accounted_bytes += allocated_bytes

            files.append({
                "filename": entry.name,
                "type": _file_type(entry.name, mode, size_bytes),
                "size_bytes": size_bytes,
                "allocated_bytes": allocated_bytes,
                "modified_at": datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat(),
            })

    files.sort(key=lambda item: (-item["allocated_bytes"], item["filename"]))
    used_bytes = usage.used
    return {
        "status": "ok",
        "scope": "top_level_files_only",
        "filesystem": {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": used_bytes,
        },
        "top_level_accounted_bytes": accounted_bytes,
        "unaccounted_bytes": used_bytes - accounted_bytes,
        "top_level_directories_not_scanned": directories_not_scanned,
        "files": files,
    }
