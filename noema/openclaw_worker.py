"""Bounded OpenClaw dispatch through its local Gateway and Docker sandbox."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .resource_control import memory_admission_reason, try_acquire

AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ALLOWED_PRIORITIES = {
    "validate_demand_and_all_in_costs", "investigate_resolution_rules",
    "repair_market_data", "require_forward_validation",
}
SANDBOX_IMAGE = "openclaw-sandbox:bookworm-slim"


@dataclass(frozen=True)
class OpenClawPolicy:
    enabled: bool
    agent_id: str = "noema-research"
    timeout_seconds: int = 120
    max_input_bytes: int = 16_384
    max_output_bytes: int = 8_192

    @classmethod
    def from_env(cls) -> OpenClawPolicy:
        return cls(
            enabled=os.getenv("NOEMA_OPENCLAW_ENABLED", "0").lower() in {"1", "true"},
            agent_id=os.getenv("NOEMA_OPENCLAW_AGENT_ID", "noema-research"),
            timeout_seconds=int(os.getenv("NOEMA_OPENCLAW_TIMEOUT_SECONDS", "120")),
        )

    def validate(self) -> None:
        if not AGENT_ID_RE.fullmatch(self.agent_id):
            raise ValueError("invalid OpenClaw agent identifier")
        if type(self.timeout_seconds) is not int or not 10 <= self.timeout_seconds <= 300:
            raise ValueError("OpenClaw timeout must be in [10,300]")


def _docker(args: list[str], *, timeout: float = 8) -> tuple[int, str]:
    executable = shutil.which("docker")
    if not executable:
        return 127, ""
    import subprocess

    allowed_env = {"PATH", "HOME", "USER", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}
    env = {key: value for key, value in os.environ.items() if key in allowed_env}
    try:
        result = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=timeout,
            check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout[:65_536]


def _gateway_id() -> str | None:
    code, output = _docker([
        "ps", "--filter", "label=com.docker.compose.service=openclaw-gateway",
        "--filter", "status=running", "--format", "{{.ID}}",
    ])
    if code != 0:
        return None
    ids = [line.strip() for line in output.splitlines() if re.fullmatch(r"[0-9a-f]{12,64}", line.strip())]
    if len(ids) != 1:
        return None
    code, output = _docker(["inspect", ids[0]])
    if code != 0:
        return None
    try:
        item = json.loads(output)[0]
    except (ValueError, IndexError, TypeError):
        return None
    if item.get("State", {}).get("Status") != "running":
        return None
    if item.get("State", {}).get("Health", {}).get("Status") != "healthy":
        return None
    return ids[0]


def _gateway_container() -> tuple[str | None, dict[str, str]]:
    """Find the single configured Gateway, whether running or stopped."""
    code, output = _docker([
        "ps", "-aq", "--filter", "label=com.docker.compose.service=openclaw-gateway",
    ])
    ids = [line.strip() for line in output.splitlines()
           if re.fullmatch(r"[0-9a-f]{12,64}", line.strip())]
    if code != 0 or len(ids) != 1:
        return None, {}
    code, output = _docker(["inspect", ids[0]])
    if code != 0:
        return None, {}
    try:
        item = json.loads(output)[0]
        labels = item["Config"]["Labels"]
        return ids[0], {
            "status": item.get("State", {}).get("Status", "unknown"),
            "project": labels["com.docker.compose.project"],
            "working_dir": labels["com.docker.compose.project.working_dir"],
            "config_files": labels["com.docker.compose.project.config_files"],
        }
    except (ValueError, IndexError, TypeError, KeyError):
        return None, {}


def _gateway_inventory_empty() -> bool:
    """Allow Compose recovery only after Docker confirms there is no Gateway."""
    code, output = _docker([
        "ps", "-aq", "--filter", "label=com.docker.compose.service=openclaw-gateway",
    ])
    return code == 0 and not output.strip()


def _configured_compose() -> dict[str, str] | None:
    """Resolve an owner-configured Compose stack without exposing its paths."""
    raw_files = os.getenv("NOEMA_OPENCLAW_COMPOSE_FILES", "")
    files = [item.strip() for item in raw_files.split(",") if item.strip()]
    if not files:
        return None
    try:
        resolved = [Path(item).expanduser().resolve(strict=True) for item in files]
    except (OSError, RuntimeError):
        return None
    if any(not path.is_file() for path in resolved):
        return None
    configured_dir = os.getenv("NOEMA_OPENCLAW_COMPOSE_PROJECT_DIRECTORY", "").strip()
    try:
        working_dir = (Path(configured_dir).expanduser().resolve(strict=True)
                       if configured_dir else resolved[0].parent)
    except (OSError, RuntimeError):
        return None
    if not working_dir.is_dir():
        return None
    project = os.getenv("NOEMA_OPENCLAW_COMPOSE_PROJECT_NAME", "").strip()
    if not project:
        project = re.sub(r"[^a-z0-9_-]+", "-", working_dir.name.lower()).strip("-_")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        return None
    return {
        "project": project,
        "working_dir": str(working_dir),
        "config_files": ",".join(str(path) for path in resolved),
    }


def _compose_command(info: dict[str, str]) -> list[str] | None:
    """Build a compose command from inspected labels or explicit owner config."""
    try:
        working_dir = Path(info["working_dir"]).resolve(strict=True)
        project = info["project"]
        files = [Path(item).resolve(strict=True)
                 for item in info["config_files"].split(",") if item]
    except (KeyError, OSError, RuntimeError):
        return None
    if not working_dir.is_dir() or not files or any(not item.is_file() for item in files):
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        return None
    args = ["compose", "--project-directory", str(working_dir), "-p", project]
    for path in files:
        args += ["-f", str(path)]
    return args


def _start_gateway() -> tuple[str | None, bool]:
    """Start the configured compose service and report whether NOEMA started it."""
    container, info = _gateway_container()
    if container and info:
        if info["status"] == "running":
            return _gateway_id(), False
        if info["status"] not in {"created", "exited"}:
            return None, False
        code, _ = _docker(["start", container], timeout=45)
        if code == 0:
            started_here = True
        else:
            command = _compose_command(info)
            if command is None:
                return None, False
            code, _ = _docker([*command, "up", "-d", "--no-build", "--no-deps",
                               "openclaw-gateway"], timeout=90)
            if code != 0:
                return None, False
            started_here = True
    else:
        # _gateway_container() also returns None for an ambiguous/multiple-Gateway
        # inventory. Never use Compose recovery to create another worker then.
        if not _gateway_inventory_empty():
            return None, False
        info = _configured_compose()
        if info is None:
            return None, False
        command = _compose_command(info)
        if command is None:
            return None, False
        code, _ = _docker([*command, "up", "-d", "--no-build", "--no-deps",
                           "openclaw-gateway"], timeout=90)
        if code != 0:
            # Compose may partially start the service before returning an error.
            # Clean up only a unique container created from this configured project.
            partial, partial_info = _gateway_container()
            if (partial and partial_info.get("project") == info["project"] and
                    partial_info.get("status") in {"created", "running", "exited"}):
                _docker(["stop", "--time", "10", partial], timeout=20)
            return None, False
        started_here = True
    import time
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        gateway = _gateway_id()
        if gateway:
            return gateway, True
        time.sleep(2)
    replacement, _info = _gateway_container()
    return (replacement, started_here) if replacement else (None, started_here)


def _exec_gateway(container: str, args: list[str], *, timeout: float = 10) -> tuple[int, str]:
    return _docker(["exec", container, *args], timeout=timeout)


def _ensure_docker_engine() -> bool | None:
    """Ensure Docker Desktop is available; return True only when NOEMA started it."""
    code, _ = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=5)
    if code == 0:
        return False
    if os.getenv("NOEMA_DOCKER_DESKTOP_AUTOSTART", "1").lower() not in {"1", "true"}:
        return None
    code, output = _docker(["desktop", "status", "--format", "json"], timeout=5)
    if code != 0:
        return None
    try:
        state = json.loads(output).get("Status")
    except (ValueError, AttributeError):
        return None
    started_here = state != "running"
    if started_here:
        code, _ = _docker(["desktop", "start", "--timeout", "180"], timeout=190)
        if code != 0:
            return None
    import time
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        code, _ = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=5)
        if code == 0:
            return started_here
        time.sleep(2)
    return None


def _stop_desktop_if_idle(started_here: bool) -> None:
    if not started_here:
        return
    code, output = _docker(["ps", "-q"], timeout=5)
    # Docker Desktop is shared infrastructure. Leave it alone when another
    # container is running or the engine state cannot be established.
    if code == 0 and not output.strip():
        _docker(["desktop", "stop", "--timeout", "60"], timeout=70)


def _sandbox_policy(container: str, agent_id: str) -> dict[str, Any] | None:
    code, output = _exec_gateway(
        container, ["openclaw", "sandbox", "explain", "--agent", agent_id, "--json"],
    )
    if code != 0:
        return None
    try:
        document = json.loads(output)
        sandbox = document["sandbox"]
        sandbox_tools = sandbox["tools"]
        elevated = document["elevated"]
        code, agent_tools_output = _exec_gateway(
            container, ["openclaw", "config", "get", f"agents.entries.{agent_id}.tools"],
        )
        if code != 0:
            return None
        agent_tools = json.loads(agent_tools_output)
        code, agent_sandbox_output = _exec_gateway(
            container, ["openclaw", "config", "get", f"agents.entries.{agent_id}.sandbox"],
        )
        if code != 0:
            return None
        agent_sandbox = json.loads(agent_sandbox_output)
        docker = agent_sandbox.get("docker", {})
        return {
            "mode": sandbox.get("mode"), "scope": sandbox.get("scope"),
            "backend": sandbox.get("backend"),
            "workspace_access": sandbox.get("workspaceAccess"),
            "network": docker.get("network", "none"),
            "sandboxed": sandbox.get("sessionIsSandboxed") is True,
            "tool_allow": agent_tools.get("allow"),
            "sandbox_tool_allow": sandbox_tools.get("allow"),
            "sandbox_tool_deny": sandbox_tools.get("deny"),
            "elevated": elevated.get("enabled") is True or
                        agent_tools.get("elevated", {}).get("enabled") is True,
        }
    except (ValueError, TypeError, KeyError):
        return None


def runtime_status(db_path: str | None = None) -> dict[str, Any]:
    """Return an allowlisted, secret-free view of the installed OpenClaw worker."""
    policy = OpenClawPolicy.from_env()
    try:
        policy.validate()
    except ValueError:
        return {"state": "invalid_config", "enabled": policy.enabled, "agent_id": None}
    result: dict[str, Any] = {
        "state": "unavailable", "enabled": policy.enabled,
        "on_demand": True,
        "agent_id": policy.agent_id, "gateway": "unavailable",
        "sandbox": "unavailable", "sandbox_ready": False,
        "active_session": None, "last_session_status": None,
        "model": None, "input_tokens": None, "output_tokens": None,
        "cost_usd": None, "reason": "OpenClaw Gateway unavailable",
    }
    container = _gateway_id()
    if not container:
        configured, details = _gateway_container()
        if configured and details.get("status") in {"created", "exited"}:
            result["state"] = "stopped"
            result["gateway"] = "stopped"
            result["reason"] = "Gateway starts only for admitted NOEMA work"
        else:
            result["reason"] = "no unique healthy local OpenClaw Gateway"
        return result
    result["gateway"] = "healthy"
    _code, output = _exec_gateway(container, ["sh", "-lc", (
        "if command -v docker >/dev/null 2>&1; then printf docker_cli=available; "
        "else printf docker_cli=unavailable; fi; "
        "if [ -S \"${OPENCLAW_DOCKER_SOCKET:-/var/run/docker.sock}\" ]; "
        "then printf ' docker_socket=available'; else printf ' docker_socket=unavailable'; fi; "
        "if docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; "
        "then printf ' docker_daemon=reachable'; else printf ' docker_daemon=unavailable'; fi"
    )])
    checks = output.strip().split()
    docker_cli = "docker_cli=available" in checks
    docker_socket = "docker_socket=available" in checks
    docker_daemon = "docker_daemon=reachable" in checks
    code_image, _ = _docker(["image", "inspect", SANDBOX_IMAGE])
    sandbox = _sandbox_policy(container, policy.agent_id)
    agent_configured = sandbox is not None
    result["sandbox"] = "configured" if agent_configured else "unavailable"
    result["sandbox_policy"] = ({key: sandbox[key] for key in (
        "mode", "scope", "backend", "workspace_access", "network", "sandboxed", "tool_allow", "elevated",
    )} if sandbox else None)
    secure_policy = bool(
        sandbox and sandbox["mode"] == "all" and sandbox["scope"] == "session"
        and sandbox["backend"] == "docker" and sandbox["workspace_access"] == "none"
        and sandbox["network"] == "none"
        and sandbox["tool_allow"] == ["exec"] and not sandbox["elevated"]
        and "exec" in (sandbox.get("sandbox_tool_allow") or [])
        and "exec" not in (sandbox.get("sandbox_tool_deny") or [])
    )
    ready = docker_cli and docker_socket and docker_daemon and code_image == 0 and secure_policy
    result["sandbox_ready"] = ready
    if not docker_cli:
        result["reason"] = "Docker CLI unavailable inside Gateway"
    elif not docker_socket:
        result["reason"] = "Docker daemon socket unavailable inside Gateway"
    elif not docker_daemon:
        result["reason"] = "Gateway cannot reach the Docker daemon"
    elif code_image != 0:
        result["reason"] = "OpenClaw sandbox image is unavailable to Docker"
    elif not secure_policy:
        result["reason"] = "dedicated worker sandbox or tool policy does not match NOEMA limits"
    else:
        result["state"] = "idle"
        result["reason"] = None
    if db_path:
        try:
            import sqlite3

            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1) as conn:
                row = conn.execute(
                    "SELECT session_id,status,model,input_tokens,output_tokens,estimated_model_cost_usd "
                    "FROM cognitive_sessions WHERE provider='openclaw' "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            if row:
                (result["active_session"], result["last_session_status"], result["model"],
                 result["input_tokens"], result["output_tokens"], result["cost_usd"]) = row
                if result["last_session_status"] == "running":
                    result["state"] = "working"
        except (OSError, sqlite3.Error):
            pass
    return result


def _extract_text(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "text" and isinstance(child, str):
                found.append(child)
            elif key in {"payload", "content", "result", "response", "messages"}:
                found.extend(_extract_text(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_extract_text(child))
    elif isinstance(value, str):
        found.append(value)
    return found


def _validate_result(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > OpenClawPolicy.max_output_bytes:
        raise ValueError("oversized worker result")
    result = json.loads(text.strip())
    if not isinstance(result, dict) or set(result) != {
        "status", "verified_metrics", "limitation", "falsification_test", "next_priority",
        "live_eligible",
    }:
        raise ValueError("invalid worker result shape")
    if result["status"] not in {"reviewed", "insufficient_evidence"}:
        raise ValueError("invalid worker status")
    if result["live_eligible"] is not False:
        raise ValueError("worker cannot grant live eligibility")
    if result["next_priority"] not in ALLOWED_PRIORITIES:
        raise ValueError("invalid worker priority")
    for field, limit in (("limitation", 240), ("falsification_test", 300)):
        if not isinstance(result[field], str) or not result[field].strip() or len(result[field]) > limit:
            raise ValueError("invalid worker explanation")
    metrics = result["verified_metrics"]
    if not isinstance(metrics, dict) or len(metrics) > 8 or any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in metrics.items()
    ):
        raise ValueError("invalid worker metrics")
    return result


def _usage(document: dict[str, Any]) -> dict[str, Any]:
    usage: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if "usage" in value and isinstance(value["usage"], dict):
                usage.update(value["usage"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    result: dict[str, Any] = {}
    for key, aliases in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
    }.items():
        for alias in aliases:
            value = usage.get(alias)
            if type(value) is int and value >= 0:
                result[key] = value
                break
    cost = usage.get("cost")
    if isinstance(cost, dict):
        cost = cost.get("total") or cost.get("total_usd")
    try:
        parsed_cost = float(cost)
    except (TypeError, ValueError):
        parsed_cost = math.nan
    result["cost_usd"] = parsed_cost if math.isfinite(parsed_cost) and parsed_cost >= 0 else None
    return result


async def run_review(*, task: str, session_key: str, policy: OpenClawPolicy | None = None,
                     container: str | None = None) -> dict[str, Any]:
    """Dispatch one strict-JSON critique only when the dedicated worker is sandbox-ready."""
    policy = policy or OpenClawPolicy.from_env()
    policy.validate()
    if not policy.enabled:
        return {"status": "disabled", "reason": "OpenClaw worker is not enabled"}
    if len(task.encode("utf-8")) > policy.max_input_bytes:
        return {"status": "rejected", "reason": "worker input exceeds its size bound"}
    lease, blocked = try_acquire("docker_worker")
    if lease is None:
        return {"status": "queued", "reason": blocked or "resource unavailable"}
    started_here = False
    desktop_started_here = False
    try:
        engine_started = await asyncio.to_thread(_ensure_docker_engine)
        if engine_started is None:
            return {"status": "blocked", "reason": "Docker Desktop is unavailable"}
        desktop_started_here = engine_started
        pressure_reason = await asyncio.to_thread(memory_admission_reason)
        if pressure_reason:
            return {"status": "queued", "reason": pressure_reason}
        initial_status = await asyncio.to_thread(runtime_status)
        startable_gateway_states = {
            "no unique healthy local OpenClaw Gateway",
            "Gateway starts only for admitted NOEMA work",
        }
        if (not initial_status.get("sandbox_ready") and
                initial_status.get("reason") not in startable_gateway_states):
            return {"status": "blocked", "reason": initial_status.get(
                "reason", "sandbox unavailable")}
        if container is None:
            container = await asyncio.to_thread(_gateway_id)
            if not container:
                container, started_here = await asyncio.to_thread(_start_gateway)
        if not container:
            status = await asyncio.to_thread(runtime_status)
            return {"status": "blocked", "reason": status.get(
                "reason", "OpenClaw Gateway could not be started safely")}
        status = await asyncio.to_thread(runtime_status)
        if not status.get("sandbox_ready"):
            return {"status": "blocked", "reason": status.get("reason", "sandbox unavailable")}
        return await _run_review_in_gateway(task, session_key, policy, container)
    finally:
        if started_here and container:
            await asyncio.to_thread(_docker, ["stop", "--time", "10", container], timeout=20)
        if desktop_started_here:
            await asyncio.to_thread(_stop_desktop_if_idle, True)
        lease.release()


async def _run_review_in_gateway(task: str, session_key: str, policy: OpenClawPolicy,
                                 container: str) -> dict[str, Any]:
    session_key = f"noema-{uuid.UUID(session_key).hex}"
    shell = (
        'task=$(mktemp /tmp/noema-openclaw-task.XXXXXX); '
        'trap "rm -f \\\"$task\\\"" EXIT; cat > "$task"; '
        f'openclaw agent --agent {policy.agent_id} --session-key {session_key} '
        f'--message-file "$task" --timeout {policy.timeout_seconds} --json'
    )
    executable = shutil.which("docker")
    if not executable:
        return {"status": "blocked", "reason": "Docker CLI unavailable to NOEMA"}
    process = await asyncio.create_subprocess_exec(
        executable, "exec", "-i", container, "sh", "-lc", shell,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(task.encode("utf-8")), timeout=policy.timeout_seconds + 15,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return {"status": "timed_out", "reason": "OpenClaw task exceeded its time bound"}
    if process.returncode != 0 or len(output) > 65_536:
        return {"status": "failed", "reason": "OpenClaw Gateway did not complete the task"}
    scope_key = f"agent:{policy.agent_id}:{session_key}"
    cleanup_code, _ = await asyncio.to_thread(
        _exec_gateway, container,
        ["openclaw", "sandbox", "recreate", "--session", scope_key, "--force"],
        timeout=30,
    )
    documents = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            documents.append(parsed)
    document = next((item for item in reversed(documents) if item.get("ok") is True), None)
    if not document:
        return {"status": "failed", "reason": "OpenClaw returned no successful task result"}
    texts = _extract_text(document.get("result", document.get("payload", document)))
    parsed_result = None
    for text in reversed(texts):
        try:
            parsed_result = _validate_result(text)
            break
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    if parsed_result is None:
        return {"status": "invalid_result", "reason": "OpenClaw result failed schema validation"}
    usage = _usage(document)
    return {
        "status": "completed", "session_key": session_key,
        "model": None, "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"), "cost_usd": usage.get("cost_usd"),
        "cleanup_status": "removed" if cleanup_code == 0 else "pending",
        "result": parsed_result,
    }
