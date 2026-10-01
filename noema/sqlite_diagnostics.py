"""Privacy-safe SQLite failure classification for operational logs."""

from __future__ import annotations

import sqlite3


def sqlite_failure_diagnostics(error: BaseException) -> dict[str, str]:
    """Return allowlisted SQLite diagnostics without exposing exception text.

    SQLite error messages can contain identifiers, paths, and bound values. Only
    fixed categories and SQLite's documented symbolic result names are emitted.
    """
    if not isinstance(error, sqlite3.Error):
        return {}

    message = str(error).strip().lower()
    if message.startswith(("database is locked", "database table is locked",
                           "database schema is locked")):
        category = "busy_or_locked"
    elif message.startswith("no such table:"):
        category = "missing_table"
    elif message.startswith(("no such column:", "has no column named ")):
        category = "missing_column"
    elif message.startswith(("disk i/o error", "unable to open database file")):
        category = "storage_io_or_open"
    elif message.startswith(("database or disk is full", "database full")):
        category = "storage_full"
    elif message.startswith("attempt to write a readonly database"):
        category = "read_only_database"
    elif message.startswith("file is not a database"):
        category = "invalid_database"
    else:
        category = "other_sqlite_error"

    raw_name = getattr(error, "sqlite_errorname", None)
    allowed_names = {
        "SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_ERROR", "SQLITE_SCHEMA",
        "SQLITE_CORRUPT", "SQLITE_NOTADB", "SQLITE_FULL", "SQLITE_IOERR",
        "SQLITE_READONLY", "SQLITE_CANTOPEN", "SQLITE_CONSTRAINT",
        "SQLITE_MISUSE", "SQLITE_INTERRUPT", "SQLITE_PERM", "SQLITE_AUTH",
    }
    result = {"sqlite_failure": category}
    if raw_name in allowed_names:
        result["sqlite_errorname"] = raw_name
    return result
