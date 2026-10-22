from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import signal
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .agent_config import AgentConfig
from .agent_runtime import run_agent
from .durable_mirror import sync_critical_state
from .local_env import load_local_env
from .storage_recovery import recover_storage
from .trading_coordinator import reconcile_scheduled_trading


@contextmanager
def _agent_pidfile(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as pidfile:
        try:
            fcntl.flock(pidfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("NOEMA agent PID lock is already held") from exc

        pidfile.seek(0)
        previous_pid = pidfile.read().strip()
        if previous_pid.isdigit():
            prior_process = subprocess.run(
                ["ps", "-p", previous_pid, "-o", "command="],
                capture_output=True, text=True, check=False, timeout=2,
            )
            command = prior_process.stdout.strip()
            if "noema.agent_entry" in command or "noema-agent" in command:
                raise RuntimeError(
                    f"NOEMA agent process {previous_pid} is already running"
                )

        pidfile.seek(0)
        pidfile.truncate()
        pidfile.write(f"{os.getpid()}\n")
        pidfile.flush()
        os.fsync(pidfile.fileno())
        try:
            yield
        finally:
            pidfile.seek(0)
            if pidfile.read().strip() == str(os.getpid()):
                pidfile.seek(0)
                pidfile.truncate()
                pidfile.flush()
                os.fsync(pidfile.fileno())
            fcntl.flock(pidfile.fileno(), fcntl.LOCK_UN)


async def _wait_for_storage(config: AgentConfig) -> None:
    retry_seconds = max(15.0, float(os.getenv("NOEMA_STORAGE_RETRY_SECONDS", "60")))
    while True:
        report = await asyncio.to_thread(recover_storage, config.db_path, role="worker")
        print(json.dumps({"event": "storage_recovery", **report.safe_fields()}, sort_keys=True), flush=True)
        try:
            mirror = await sync_critical_state(config.db_path)
        except Exception as exc:  # noqa: BLE001 - emergency mirror must never hide storage status.
            mirror = {"status": "unavailable", "error_type": type(exc).__name__}
        print(json.dumps(
            {"event": "storage_emergency_durable_mirror", **mirror},
            sort_keys=True,
        ), flush=True)
        if report.safe_to_write:
            return
        await asyncio.sleep(retry_seconds)


def _recent_processes(window_s: float = 15.0) -> list[str]:
    """Commands (redacted, truncated, no env) of processes started in the last window_s seconds."""
    try:
        hz = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        out = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                start = int((d / "stat").read_text().rsplit(")", 1)[1].split()[19]) / hz
                if uptime - start <= window_s:
                    cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                    cmd = re.sub(r"(?i)(key|token|secret|password|bearer)([=: ]+)\S+", r"\1\2***", cmd)
                    out.append(f"{d.name}: {cmd[:80]} … {cmd[-160:]}" if len(cmd) > 240 else f"{d.name}: {cmd}")
            except (OSError, IndexError, ValueError):
                continue
        return out[:20]
    except (OSError, ValueError):
        return []


def main() -> None:
    os.chdir(Path(__file__).resolve().parents[1])
    load_local_env()
    config = AgentConfig.from_env()
    default_pid = "/tmp/noema-agent.pid" if Path(config.db_path).is_absolute() else "data/agent.pid"
    pid_path = Path(os.getenv("NOEMA_AGENT_PID_PATH", default_pid))
    if not pid_path.is_absolute():
        pid_path = Path.cwd() / pid_path

    async def run_with_shutdown_signal() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        def _on_sigterm() -> None:
            # Who is stopping us? Log recently-started processes (the sender is usually a
            # short-lived kill/pkill or a supervisor) so unexplained restarts are attributable.
            print(json.dumps({"event": "agent_sigterm_received", "ppid": os.getppid(),
                              "recent_processes": _recent_processes()}, sort_keys=True), flush=True)
            task.cancel()

        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        try:
            await _wait_for_storage(config)
            await run_agent(config)
        except asyncio.CancelledError:
            pass
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            try:
                await reconcile_scheduled_trading(config.db_path)
            except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
                print(json.dumps({
                    "event": "trading_coordinator_shutdown_reconcile",
                    "status": "skipped",
                }, sort_keys=True), flush=True)

    with _agent_pidfile(pid_path):
        asyncio.run(run_with_shutdown_signal())


if __name__ == "__main__":
    main()
