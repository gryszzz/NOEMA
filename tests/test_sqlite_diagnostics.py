import sqlite3

import pytest

from noema.sqlite_diagnostics import sqlite_failure_diagnostics


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("database is locked: /private/account.db", "busy_or_locked"),
        ("no such table: wallet_events", "missing_table"),
        ("no such column: private_value", "missing_column"),
        ("database or disk is full", "storage_full"),
        ("attempt to write a readonly database", "read_only_database"),
        ("file is not a database", "invalid_database"),
        ("unrecognized token: sensitive-value", "other_sqlite_error"),
    ],
)
def test_sqlite_error_diagnostics_are_categorized_without_message_leak(
    message: str, category: str,
) -> None:
    error = sqlite3.OperationalError(message)
    diagnostics = sqlite_failure_diagnostics(error)
    assert diagnostics["sqlite_failure"] == category
    assert message not in repr(diagnostics)
    assert "private/account.db" not in repr(diagnostics)
    assert "sensitive-value" not in repr(diagnostics)


def test_sqlite_symbolic_error_name_is_allowlisted() -> None:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorname = "SQLITE_BUSY"
    assert sqlite_failure_diagnostics(error) == {
        "sqlite_failure": "busy_or_locked",
        "sqlite_errorname": "SQLITE_BUSY",
    }


def test_non_sqlite_error_produces_no_sqlite_details() -> None:
    assert sqlite_failure_diagnostics(RuntimeError("secret")) == {}
