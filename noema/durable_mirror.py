from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_STREAMS = (
    "agent_runtime",
    "agent_heartbeats",
    "agent_cycle_timings",
    "runtime_events",
    "missions",
    "mission_events",
    "mission_handoffs",
    "ecosystem_specialists",
    "ecosystem_reviews",
    "research_trials",
    "autonomous_research_runs",
    "forecast_ledger",
    "economic_events",
    "economic_provider_coverage",
    "economic_reserve_attestations",
    "economic_counterfactuals",
    "bill_budget",
    "bill_entries",
    "prediction_account_records",
    "prediction_account_sync_state",
)

_TIMESTAMP_FIELDS = (
    "occurred_at",
    "created_at",
    "updated_at",
    "completed_at",
    "recorded_at",
    "last_observed_at",
)


@dataclass(frozen=True)
class DurableMirrorConfig:
    url: str
    token: str
    enabled: bool = True
    timeout_seconds: float = 10.0
    rows_per_stream: int = 200

    @classmethod
    def from_env(cls) -> "DurableMirrorConfig":
        url = os.getenv("NOEMA_DURABLE_MIRROR_URL", "").strip()
        token = os.getenv("NOEMA_DURABLE_MIRROR_TOKEN", "").strip()
        enabled = os.getenv("NOEMA_DURABLE_MIRROR_ENABLED", "1").strip().lower() not in {
            "0", "false", "no",
        }
        try:
            timeout = max(2.0, min(60.0, float(
                os.getenv("NOEMA_DURABLE_MIRROR_TIMEOUT_SECONDS", "10")
            )))
        except ValueError:
            timeout = 10.0
        try:
            rows = max(10, min(500, int(
                os.getenv("NOEMA_DURABLE_MIRROR_ROWS_PER_STREAM", "200")
            )))
        except ValueError:
            rows = 200
        return cls(url=url, token=token, enabled=enabled, timeout_seconds=timeout,
                   rows_per_stream=rows)

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.url and self.token)


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__noema_bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_commit() -> str:
    for key in ("RAILWAY_GIT_COMMIT_SHA", "RENDER_GIT_COMMIT", "GITHUB_SHA"):
        value = os.getenv(key, "").strip()
        if value:
            return value[:80]
    return "unknown"


def _source_host() -> str:
    for key in ("RAILWAY_SERVICE_NAME", "RENDER_SERVICE_NAME"):
        value = os.getenv(key, "").strip()
        if value:
            return value[:120]
    return platform.node()[:120] or "unknown"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _occurred_at(payload: dict[str, Any]) -> str | None:
    for key in _TIMESTAMP_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _stream_rows(
    conn: sqlite3.Connection,
    stream: str,
    *,
    after_rowid: int,
    limit: int,
) -> list[dict[str, Any]]:
    quoted = '"' + stream.replace('"', '""') + '"'
    cursor = conn.execute(
        f"SELECT rowid AS _mirror_rowid, * FROM {quoted} "
        "WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
        (after_rowid, limit),
    )
    names = [item[0] for item in cursor.description]
    result = []
    for raw in cursor.fetchall():
        row = dict(zip(names, raw))
        rowid = int(row.pop("_mirror_rowid"))
        payload = _json_safe(row)
        result.append({
            "stream": stream,
            "record_key": f"{stream}:{rowid}",
            "version_sha256": _canonical_hash(payload),
            "occurred_at": _occurred_at(payload),
            "payload": payload,
            "source_commit": _source_commit(),
            "source_host": _source_host(),
            "_rowid": rowid,
        })
    return result


async def _post(
    client: httpx.AsyncClient,
    config: DurableMirrorConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        config.url,
        headers={"x-noema-mirror-token": config.token},
        json=payload,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {"status": "invalid_response"}


async def mirror_health(
    config: DurableMirrorConfig | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    config = config or DurableMirrorConfig.from_env()
    if not config.ready:
        return {"status": "disabled_or_unconfigured"}
    async with httpx.AsyncClient(
        timeout=config.timeout_seconds, transport=transport,
    ) as client:
        return await _post(client, config, {"action": "health"})


async def sync_critical_state(
    db_path: str,
    config: DurableMirrorConfig | None = None,
    *,
    streams: tuple[str, ...] = DEFAULT_STREAMS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    config = config or DurableMirrorConfig.from_env()
    if not config.ready:
        return {"status": "disabled_or_unconfigured", "mirrored": 0, "streams": 0}
    database = Path(db_path)
    if not database.is_file():
        return {"status": "database_unavailable", "mirrored": 0, "streams": 0}

    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        present = tuple(stream for stream in streams if stream in _tables(conn))
        if not present:
            return {"status": "no_streams", "mirrored": 0, "streams": 0}

        async with httpx.AsyncClient(
            timeout=config.timeout_seconds, transport=transport,
        ) as client:
            checkpoint_response = await _post(
                client, config, {"action": "checkpoints", "streams": list(present)},
            )
            checkpoints = checkpoint_response.get("checkpoints")
            if not isinstance(checkpoints, dict):
                checkpoints = {}

            mirrored = 0
            touched = 0
            lagging = 0
            for stream in present:
                raw_checkpoint = checkpoints.get(stream)
                cursor = 0
                if isinstance(raw_checkpoint, dict):
                    try:
                        cursor = max(0, int(raw_checkpoint.get("last_cursor") or 0))
                    except (TypeError, ValueError):
                        cursor = 0

                rows = _stream_rows(
                    conn, stream, after_rowid=cursor, limit=config.rows_per_stream,
                )
                if not rows:
                    continue
                touched += 1
                last_rowid = int(rows[-1].pop("_rowid"))
                for row in rows[:-1]:
                    row.pop("_rowid", None)
                payload_rows = [
                    {key: value for key, value in row.items() if key != "_rowid"}
                    for row in rows
                ]
                result = await _post(
                    client,
                    config,
                    {
                        "action": "ingest",
                        "records": payload_rows,
                        "checkpoint": {
                            "stream": stream,
                            "last_cursor": str(last_rowid),
                            "metadata": {
                                "source_commit": _source_commit(),
                                "source_host": _source_host(),
                            },
                        },
                    },
                )
                mirrored += int(result.get("accepted") or 0)
                if len(rows) >= config.rows_per_stream:
                    lagging += 1

            return {
                "status": "persisted",
                "mirrored": mirrored,
                "streams": touched,
                "lagging_streams": lagging,
                "present_streams": len(present),
            }
    finally:
        conn.close()
