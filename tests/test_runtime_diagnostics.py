from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from noema.runtime_diagnostics import (
    RuntimeProcessIdentity,
    _process_database_state,
    capture_process_identity,
    diagnose_runtime,
    probe_local_process,
)

NOW = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)
DEFAULT_IDENTITY = object()


def identity(tmp_path, **changes):
    result = {
        "pid": 123,
        "started_at": (NOW-timedelta(minutes=1)).isoformat(),
        "checkout_path": str(tmp_path.resolve()),
        "commit": "a" * 40,
        "database_path": str((tmp_path / "noema.db").resolve()),
    }
    result.update(changes)
    return result


def diagnose(tmp_path, *, heartbeat_age=1, process_state="PROCESS MATCH",
             identity_value=DEFAULT_IDENTITY,
             database_path=None, checkout_path=None):
    return diagnose_runtime(
        running=True,
        heartbeat_age_seconds=heartbeat_age,
        identity_value=(identity(tmp_path) if identity_value is DEFAULT_IDENTITY
                        else identity_value),
        database_path=str(database_path or tmp_path / "noema.db"),
        checkout_path=str(checkout_path or tmp_path),
        now=NOW,
        process_probe=lambda _identity, _now: process_state,
    )


def test_heartbeat_alone_never_claims_runtime_is_live(tmp_path):
    result = diagnose(tmp_path, identity_value=None)
    assert result["state"] == "UNKNOWN"
    assert result["heartbeat_fresh"] is True


def test_old_heartbeat_without_process_identity_is_stale(tmp_path):
    result = diagnose(tmp_path, heartbeat_age=120, identity_value=None)
    assert result["state"] == "STALE"


@pytest.mark.parametrize(
    ("process_state", "expected"),
    [
        ("PROCESS MISSING", "PROCESS MISSING"),
        ("PID MISMATCH", "PID MISMATCH"),
        ("WRONG CHECKOUT", "WRONG CHECKOUT"),
    ],
)
def test_process_probe_failures_never_report_live(tmp_path, process_state, expected):
    result = diagnose(tmp_path, process_state=process_state)
    assert result["state"] == expected


def test_dead_pid_with_fresh_persisted_state_is_not_live(tmp_path):
    result = diagnose(tmp_path, heartbeat_age=1, process_state="PROCESS MISSING")
    assert result["heartbeat_fresh"] is True
    assert result["state"] == "PROCESS MISSING"


def test_live_process_with_old_heartbeat_is_heartbeat_stuck(tmp_path):
    result = diagnose(tmp_path, heartbeat_age=120)
    assert result["state"] == "HEARTBEAT STUCK"


def test_live_process_with_fresh_heartbeat_is_live(tmp_path):
    result = diagnose(tmp_path)
    assert result["state"] == "LIVE"


def test_database_and_checkout_mismatches_are_explicit(tmp_path):
    wrong_database = diagnose(tmp_path, identity_value=identity(
        tmp_path, database_path=str((tmp_path / "other.db").resolve()),
    ))
    wrong_checkout = diagnose(tmp_path, checkout_path=tmp_path / "other-checkout")
    assert wrong_database["state"] == "WRONG DATABASE"
    assert wrong_checkout["state"] == "WRONG CHECKOUT"


def test_dirty_checkout_state_is_preserved_for_dashboard_visibility(tmp_path):
    result = diagnose(tmp_path, identity_value=identity(
        tmp_path, working_tree_dirty=True,
    ))
    assert result["state"] == "LIVE"
    assert result["process_identity"]["working_tree_dirty"] is True


def test_pid_reuse_is_detected_from_process_start_time(tmp_path, monkeypatch):
    process_identity = RuntimeProcessIdentity(
        pid=123,
        started_at=(NOW - timedelta(minutes=1)).isoformat(),
        checkout_path=str(tmp_path.resolve()),
        commit="a" * 40,
        database_path=str((tmp_path / "noema.db").resolve()),
    )

    class Result:
        returncode = 0
        stdout = (
            (NOW - timedelta(seconds=10)).astimezone().strftime("%a %b %d %H:%M:%S %Y")
            + " noema-agent"
        )

    monkeypatch.setattr("noema.runtime_diagnostics.subprocess.run", lambda *a, **k: Result())
    monkeypatch.setattr("noema.runtime_diagnostics._process_cwd", lambda _pid: str(tmp_path.resolve()))
    monkeypatch.setattr("noema.runtime_diagnostics._process_database_state", lambda *_: "MATCH")

    assert probe_local_process(process_identity, NOW) == "PID MISMATCH"


def test_process_probe_accepts_matching_identity_and_open_database(tmp_path, monkeypatch):
    process_identity = RuntimeProcessIdentity(
        pid=123,
        started_at=NOW.astimezone().isoformat(),
        checkout_path=str(tmp_path.resolve()),
        commit="a" * 40,
        database_path=str((tmp_path / "noema.db").resolve()),
    )

    class Result:
        returncode = 0
        stdout = NOW.astimezone().strftime("%a %b %d %H:%M:%S %Y") + " noema-agent"

    monkeypatch.setattr("noema.runtime_diagnostics.subprocess.run", lambda *a, **k: Result())
    monkeypatch.setattr("noema.runtime_diagnostics._process_cwd", lambda _pid: str(tmp_path.resolve()))
    monkeypatch.setattr("noema.runtime_diagnostics._process_database_state", lambda *_: "MATCH")

    assert probe_local_process(process_identity, NOW) == "PROCESS MATCH"


def test_process_must_have_the_recorded_database_open(tmp_path, monkeypatch):
    class Result:
        returncode = 0
        stdout = f"p123\nn{tmp_path / 'other.db'}\n"

    monkeypatch.setattr("noema.runtime_diagnostics.subprocess.run", lambda *a, **k: Result())
    assert _process_database_state(123, str(tmp_path / "noema.db")) == "WRONG DATABASE"


def test_malformed_and_future_heartbeats_stay_unknown(tmp_path):
    malformed = diagnose(tmp_path, identity_value={"pid": "bad"})
    future = diagnose(tmp_path, heartbeat_age=-1)
    assert malformed["state"] == "UNKNOWN"
    assert future["state"] == "UNKNOWN"


def test_capture_records_process_checkout_revision_and_database(tmp_path):
    database = tmp_path / "noema.db"
    captured = capture_process_identity(str(database))

    assert captured.pid > 0
    assert captured.started_at
    assert Path(captured.checkout_path).is_absolute()
    assert captured.commit is None or len(captured.commit) == 40
    assert captured.database_path == str(database.resolve())
