from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import httpx

_ADDRESS_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+:[0-9]{1,5}$")
_COMMIT_PATTERN = re.compile(r"^(?:[a-fA-F0-9]{7,64}|unknown)$")


def _snapshot_image(db_path: str) -> bytes:
    source_path = Path(db_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError("worker database is not initialized")
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as snapshot:
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=5) as source:
            source.execute("PRAGMA query_only=ON")
            with sqlite3.connect(snapshot.name) as target:
                source.backup(target)
        snapshot.flush()
        return gzip.compress(Path(snapshot.name).read_bytes(), compresslevel=6)


def _worker_metadata_header() -> str:
    """Encode only non-secret worker configuration needed to label its snapshot."""
    from .cloudflare_config import CloudflareConfig
    from .cognition_config import cognition_config_from_env, cognition_provider_name
    from .openai_config import OpenAIConfig

    try:
        config = cognition_config_from_env()
        provider = cognition_provider_name(config)
        deployment = config.model if isinstance(config, (OpenAIConfig, CloudflareConfig)) else config.deployment
        metadata = {
            "worker_commit": os.getenv("RENDER_GIT_COMMIT", "unknown")[:64],
            "cognition": {
                "provider": provider[:80],
                "deployment": str(deployment)[:200],
                "enabled": bool(config.enabled),
                "configured": bool(config.ready),
            },
        }
    except (RuntimeError, ValueError, TypeError, AttributeError):
        metadata = {"worker_commit": os.getenv("RENDER_GIT_COMMIT", "unknown")[:64],
                    "cognition": {"provider": "unknown", "deployment": None,
                                  "enabled": None, "configured": None}}
    encoded = base64.urlsafe_b64encode(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).decode("ascii")
    return encoded


def decode_worker_metadata(encoded: str) -> dict[str, object]:
    if len(encoded) > 8192:
        raise ValueError("worker metadata is too large")
    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        metadata = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("worker metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise TypeError("worker metadata must be an object")
    commit = metadata.get("worker_commit")
    cognition = metadata.get("cognition")
    if not isinstance(commit, str) or not _COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("worker commit is invalid")
    if not isinstance(cognition, dict):
        raise TypeError("worker cognition metadata is invalid")
    provider = cognition.get("provider")
    deployment = cognition.get("deployment")
    enabled = cognition.get("enabled")
    configured = cognition.get("configured")
    if not isinstance(provider, str) or len(provider) > 80:
        raise ValueError("worker cognition provider is invalid")
    if deployment is not None and (not isinstance(deployment, str) or len(deployment) > 200):
        raise ValueError("worker cognition deployment is invalid")
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("worker cognition enabled flag is invalid")
    if configured is not None and type(configured) is not bool:
        raise ValueError("worker cognition readiness flag is invalid")
    return {
        "worker_commit": commit,
        "cognition": {
            "provider": provider,
            "deployment": deployment,
            "enabled": enabled,
            "configured": configured,
        },
    }


def persist_worker_metadata(db_path: str, metadata: dict[str, object]) -> None:
    destination = Path(db_path).resolve().with_name(Path(db_path).name + ".worker.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False,
        ) as temporary:
            temporary_path = temporary.name
            json.dump(metadata, temporary, sort_keys=True, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o444)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def load_worker_metadata(db_path: str) -> dict[str, object] | None:
    path = Path(db_path).resolve().with_name(Path(db_path).name + ".worker.json")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        # Revalidate persisted data before exposing it to the diagnostics UI.
        return decode_worker_metadata(base64.urlsafe_b64encode(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).decode("ascii"))
    except (TypeError, ValueError):
        return None


def _snapshot_publish_failure(stage: str, exc: Exception) -> dict[str, Any]:
    status_code = None
    if isinstance(exc, httpx.HTTPStatusError):
        candidate = exc.response.status_code
        status_code = candidate if isinstance(candidate, int) else None
        classification = "private_console_http_rejection"
    elif isinstance(exc, httpx.RequestError):
        classification = "private_console_transport_failure"
    elif isinstance(exc, sqlite3.Error):
        classification = "worker_snapshot_database_failure"
    elif isinstance(exc, OSError):
        classification = "worker_snapshot_storage_failure"
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        classification = "worker_snapshot_payload_failure"
    else:
        classification = "snapshot_publish_failure"
    # Never include the exception message, URL, headers, or request/response
    # content. Render's worker log needs a category and stage, not secret data.
    return {
        "status": "unavailable",
        "failure_stage": stage,
        "failure_classification": classification,
        "error_type": type(exc).__name__,
        "http_status": status_code,
    }


async def publish_console_snapshot(db_path: str) -> dict[str, Any]:
    """Push a consistent, compressed SQLite backup to the private console replica."""
    address = os.getenv("NOEMA_CONSOLE_INTERNAL_ADDRESS", "").strip()
    token = os.getenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "")
    if not address and not token:
        return {"status": "disabled"}
    if not address or not token or not _ADDRESS_PATTERN.fullmatch(address):
        return {"status": "misconfigured", "failure_stage": "configuration"}
    try:
        payload = await asyncio.to_thread(_snapshot_image, db_path)
    except Exception as exc:  # noqa: BLE001 - isolate snapshot failures; log only safe classification.
        return _snapshot_publish_failure("snapshot_backup", exc)
    try:
        metadata_header = _worker_metadata_header()
    except Exception as exc:  # noqa: BLE001 - metadata errors must not abort the worker cycle.
        return _snapshot_publish_failure("worker_metadata", exc)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            response = await client.post(
                f"http://{address}/internal/snapshot",
                content=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/gzip",
                    "X-NOEMA-Worker-Metadata": metadata_header,
                },
            )
        response.raise_for_status()
        return {"status": "persisted"}
    except Exception as exc:  # noqa: BLE001 - provider client errors are reduced to safe fields.
        return _snapshot_publish_failure("private_console_post", exc)
