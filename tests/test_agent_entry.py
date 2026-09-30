from __future__ import annotations

import os

import pytest

from noema.agent_entry import _agent_pidfile


def test_pidfile_replaces_dead_pid_and_clears_after_graceful_shutdown(tmp_path):
    path = tmp_path / "agent.pid"
    path.write_text("2147483647\n", encoding="utf-8")

    with _agent_pidfile(path):
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())

    assert path.read_text(encoding="utf-8") == ""


def test_pidfile_lock_prevents_duplicate_agent_start(tmp_path):
    path = tmp_path / "agent.pid"

    with (
        pytest.raises(RuntimeError, match="PID lock is already held"),
        _agent_pidfile(path),
        _agent_pidfile(path),
    ):
        pass
