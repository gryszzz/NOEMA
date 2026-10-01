import asyncio
import sqlite3

from noema import agent_runtime
from noema.agent_config import AgentConfig
from noema.agent_runtime import bootstrap_hosted_bill_budget
from noema.bill_tracker import BillTracker


def test_agent_config_allows_registry_based_evm_configuration() -> None:
    config = AgentConfig(evm_rpc_url="https://rpc.example")
    config.validate()


def test_collector_change_triggers_serialized_console_snapshot() -> None:
    async def run() -> None:
        changed = asyncio.Event()
        publish_lock = asyncio.Lock()
        await publish_lock.acquire()
        published = asyncio.Event()
        calls = []

        async def publish(path: str) -> dict[str, object]:
            calls.append(path)
            published.set()
            return {"status": "persisted"}

        task = asyncio.create_task(agent_runtime._changed_console_snapshot_loop(
            "worker.sqlite3", changed, publish_lock, publish,
            debounce_seconds=0, min_interval_seconds=0,
        ))
        changed.set()
        await asyncio.sleep(0)
        assert calls == []
        publish_lock.release()
        await asyncio.wait_for(published.wait(), timeout=1)
        assert calls == ["worker.sqlite3"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_polymarket_rest_sample_requests_snapshot_only_for_persisted_changes(monkeypatch) -> None:
    async def run(records_persisted: int) -> list[bool]:
        changes = []

        async def sample(_path: str) -> dict[str, object]:
            return {
                "status": "authenticated_read_only",
                "records_persisted": records_persisted,
            }

        async def stop(_seconds: int) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(agent_runtime, "sample_polymarket_private_account", sample)
        monkeypatch.setattr(agent_runtime.asyncio, "sleep", stop)
        try:
            await agent_runtime._polymarket_account_sampler(
                "worker.sqlite3", lambda: changes.append(True),
            )
        except asyncio.CancelledError:
            pass
        return changes

    assert asyncio.run(run(3)) == [True]
    assert asyncio.run(run(0)) == []


def test_trench_health_sqlite_failure_logs_only_safe_error_metadata(monkeypatch, capsys) -> None:
    monkeypatch.setenv("NOEMA_TRENCH_ENABLED", "1")
    error = sqlite3.OperationalError("database is locked: /private/account.db")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error.sqlite_errorname = "SQLITE_BUSY"

    async def fail_collection(**_kwargs):
        raise error

    monkeypatch.setattr(agent_runtime, "collect_trench_cycle", fail_collection)
    state = asyncio.run(agent_runtime._trench_state("worker.sqlite3"))

    assert state.status == "degraded"
    output = capsys.readouterr().out
    assert '"event": "agent_trench_collection_error"' in output
    assert '"sqlite_error_name": "SQLITE_BUSY"' in output
    assert '"sqlite_error_code": 5' in output
    assert "/private/account.db" not in output
    assert "database is locked" not in output


def test_render_budget_bootstrap_runs_only_for_hosted_runtime_and_persists_once(
    tmp_path, monkeypatch,
):
    db = str(tmp_path / "render.db")
    monkeypatch.delenv("RENDER", raising=False)
    assert bootstrap_hosted_bill_budget(db) is None
    assert not (tmp_path / "render.db").exists()

    monkeypatch.setenv("RENDER", "true")
    values = {
        "NOEMA_HOSTED_BILL_BUDGET_HOSTING_USD": "18.00",
        "NOEMA_HOSTED_BILL_BUDGET_OTHER_USD": "6.00",
        "NOEMA_HOSTED_BILL_BUDGET_MODEL_USD": "0.50",
        "NOEMA_HOSTED_BILL_BUDGET_OWNER_LIMIT_USD": "28.00",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert bootstrap_hosted_bill_budget(db) == "initialized"

    tracker = BillTracker(db)
    assert tracker.overview()["hosting_estimate_usd"] == "18.00"
    tracker.conn.close()
    monkeypatch.setenv("NOEMA_HOSTED_BILL_BUDGET_HOSTING_USD", "99.00")
    assert bootstrap_hosted_bill_budget(db) == "persisted_budget_retained"
    tracker = BillTracker(db)
    assert tracker.overview()["hosting_estimate_usd"] == "18.00"
    tracker.conn.close()
