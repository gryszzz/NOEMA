import asyncio
import json
import uuid

import pytest

from noema import openclaw_worker, resource_control
from noema.research_session import SessionStore


def test_dispatch_fails_closed_before_any_gateway_invocation(monkeypatch, tmp_path):
    invoked = False

    def unavailable(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("must not dispatch")

    monkeypatch.setenv("NOEMA_OPENCLAW_ENABLED", "1")
    monkeypatch.setenv("NOEMA_RESOURCE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(resource_control, "memory_snapshot", lambda: {
        "available_percent": 80, "source": "test",
    })
    monkeypatch.setattr(openclaw_worker, "runtime_status", lambda: {
        "sandbox_ready": False, "reason": "Docker daemon unavailable",
    })
    monkeypatch.setattr(openclaw_worker, "_gateway_id", unavailable)
    monkeypatch.setattr(openclaw_worker, "_ensure_docker_engine", lambda: False)
    monkeypatch.setattr(openclaw_worker, "_start_gateway", lambda: (None, False))
    result = asyncio.run(openclaw_worker.run_review(
        task="bounded review", session_key=str(uuid.uuid4()),
    ))
    assert result == {"status": "blocked", "reason": "Docker daemon unavailable"}
    assert not invoked


def test_openclaw_session_persists_result_usage_cost_and_activity(tmp_path):
    path = str(tmp_path / "noema.db")
    store = SessionStore(path)
    session_id = store.begin_worker("bounded paper-only review", model="openclaw")
    store.event(session_id, "worker_dispatch", "completed", "Structured review",
                tool="openclaw", cost_usd=0.001)
    store.learn(session_id, "trial-1", "evidence-hash", {
        "status": "reviewed", "next_priority": "require_forward_validation",
    })
    store.finish(session_id, "completed", {"status": "reviewed"}, input_tokens=17,
                 output_tokens=9, cost_usd=0.001, model="openclaw")
    row = store.conn.execute(
        "SELECT status,provider,model,input_tokens,output_tokens,estimated_model_cost_usd "
        "FROM cognitive_sessions WHERE session_id=?", (session_id,),
    ).fetchone()
    assert row == ("completed", "openclaw", "openclaw", 17, 9, 0.001)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM runtime_events WHERE session_id=?", (session_id,),
    ).fetchone()[0] == 2
    assert store.conn.execute(
        "SELECT trial_id,next_priority FROM research_lessons WHERE session_id=?", (session_id,),
    ).fetchone() == ("trial-1", "require_forward_validation")
    store.conn.close()


def test_result_schema_never_allows_worker_to_grant_live_eligibility():
    document = {
        "status": "reviewed", "verified_metrics": {}, "limitation": "No new evidence.",
        "falsification_test": "Repeat with forward observations.",
        "next_priority": "require_forward_validation", "live_eligible": True,
    }
    with pytest.raises(ValueError, match="live eligibility"):
        openclaw_worker._validate_result(json.dumps(document))


def test_missing_gateway_uses_runtime_compose_files_and_derived_project(tmp_path, monkeypatch):
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setenv("NOEMA_OPENCLAW_COMPOSE_FILES", str(compose))
    monkeypatch.delenv("NOEMA_OPENCLAW_COMPOSE_PROJECT_DIRECTORY", raising=False)
    monkeypatch.delenv("NOEMA_OPENCLAW_COMPOSE_PROJECT_NAME", raising=False)

    info = openclaw_worker._configured_compose()
    assert info == {
        "project": tmp_path.name.lower(),
        "working_dir": str(tmp_path.resolve()),
        "config_files": str(compose.resolve()),
    }
    assert openclaw_worker._compose_command(info) == [
        "compose", "--project-directory", str(tmp_path.resolve()), "-p", tmp_path.name.lower(),
        "-f", str(compose.resolve()),
    ]


def test_missing_gateway_compose_start_failure_fails_closed(monkeypatch, tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setenv("NOEMA_OPENCLAW_ENABLED", "1")
    monkeypatch.setenv("NOEMA_OPENCLAW_COMPOSE_FILES", str(compose))
    monkeypatch.setattr(openclaw_worker, "_gateway_container", lambda: (None, {}))
    monkeypatch.setattr(openclaw_worker, "_gateway_id", lambda: None)
    monkeypatch.setattr(openclaw_worker, "_ensure_docker_engine", lambda: False)
    monkeypatch.setattr(openclaw_worker, "memory_admission_reason", lambda: None)
    calls = []

    def docker(args, *, timeout=8):
        calls.append(args)
        if args[:2] == ["ps", "-aq"]:
            return 0, ""
        return (1, "") if "up" in args else (0, "")

    monkeypatch.setattr(openclaw_worker, "_docker", docker)
    monkeypatch.setattr(openclaw_worker, "runtime_status", lambda: {
        "sandbox_ready": False, "reason": "no unique healthy local OpenClaw Gateway",
    })
    monkeypatch.setattr(openclaw_worker, "_run_review_in_gateway", lambda *_args: pytest.fail(
        "worker dispatch must not happen after Compose startup failure"))

    class Lease:
        def release(self):
            pass

    monkeypatch.setattr(openclaw_worker, "try_acquire", lambda _kind: (Lease(), None))
    result = asyncio.run(openclaw_worker.run_review(
        task="bounded review", session_key=str(uuid.uuid4()),
    ))

    assert result["status"] == "blocked"
    assert result["reason"] == "no unique healthy local OpenClaw Gateway"
    compose_calls = [args for args in calls if args and args[0] == "compose"]
    assert len(compose_calls) == 1
    assert compose_calls[0][-5:] == ["up", "-d", "--no-build", "--no-deps", "openclaw-gateway"]


def test_ambiguous_gateway_inventory_never_starts_another_compose_worker(monkeypatch, tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setenv("NOEMA_OPENCLAW_COMPOSE_FILES", str(compose))
    monkeypatch.setattr(openclaw_worker, "_gateway_container", lambda: (None, {}))
    calls = []

    def docker(args, *, timeout=8):
        calls.append(args)
        return 0, "0123456789ab\nabcdef012345\n"

    monkeypatch.setattr(openclaw_worker, "_docker", docker)
    assert openclaw_worker._start_gateway() == (None, False)
    assert all(args[0] != "compose" for args in calls)
