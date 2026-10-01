import sqlite3

from noema.sqlite_diagnostics import sqlite_error_fields


def test_sqlite_failure_metadata_exposes_only_result_code_and_name():
    exc = sqlite3.OperationalError("database is locked; secret-row-placeholder")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    exc.sqlite_errorname = "SQLITE_BUSY"

    assert sqlite_error_fields(exc) == {
        "sqlite_error_code": sqlite3.SQLITE_BUSY,
        "sqlite_error_name": "SQLITE_BUSY",
    }
    assert "secret-row-placeholder" not in str(sqlite_error_fields(exc))


def test_sqlite_failure_metadata_omits_untrusted_names_and_non_sqlite_errors():
    exc = sqlite3.OperationalError("sensitive detail")
    exc.sqlite_errorcode = "5"
    exc.sqlite_errorname = "SQLITE_BUSY; sensitive detail"

    assert sqlite_error_fields(exc) == {
        "sqlite_error_code": None,
        "sqlite_error_name": None,
    }
    assert sqlite_error_fields(RuntimeError("sensitive detail")) == {}
