"""Secret-safe SQLite failure metadata for runtime diagnostics."""

from __future__ import annotations

import re
import sqlite3

_SQLITE_ERROR_NAME = re.compile(r"^SQLITE_[A-Z0-9_]{1,48}$")


def sqlite_error_fields(exc: BaseException) -> dict[str, int | str | None]:
    """Expose SQLite result code/name, never the exception message."""
    if not isinstance(exc, sqlite3.Error):
        return {}
    code = getattr(exc, "sqlite_errorcode", None)
    name = getattr(exc, "sqlite_errorname", None)
    return {
        "sqlite_error_code": code if type(code) is int else None,
        "sqlite_error_name": name if isinstance(name, str) and _SQLITE_ERROR_NAME.fullmatch(name) else None,
    }
