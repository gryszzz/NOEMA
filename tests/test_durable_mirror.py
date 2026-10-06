from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from noema.durable_mirror import DurableMirrorConfig, sync_critical_state


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE missions (mission_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO missions(mission_id,status,created_at) VALUES ('m1','running','2026-10-06T16:00:00+00:00')"
        )
        conn.execute(
            "CREATE TABLE economic_events (id INTEGER PRIMARY KEY, provider TEXT, amount TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO economic_events(id,provider,amount,created_at) VALUES (1,'openai','1.25','2026-10-06T16:01:00+00:00')"
        )
        conn.commit()


@pytest.mark.asyncio
async def test_sync_critical_state_uses_remote_checkpoints_and_hashes_rows(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    _db(db)
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["x-noema-mirror-token"] == "t" * 40
        if body["action"] == "checkpoints":
            return httpx.Response(200, json={"status": "ready", "checkpoints": {}})
        if body["action"] == "ingest":
            records = body["records"]
            assert records
            for record in records:
                encoded = json.dumps(
                    record["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode()
                assert record["version_sha256"] == hashlib.sha256(encoded).hexdigest()
            return httpx.Response(200, json={"status": "persisted", "accepted": len(records), "duplicates": 0})
        raise AssertionError(body)

    result = await sync_critical_state(
        str(db),
        DurableMirrorConfig(url="https://mirror.test", token="t" * 40, rows_per_stream=50),
        streams=("missions", "economic_events"),
        transport=httpx.MockTransport(handler),
    )

    assert result["status"] == "persisted"
    assert result["mirrored"] == 2
    assert [call["action"] for call in calls] == ["checkpoints", "ingest", "ingest"]


@pytest.mark.asyncio
async def test_sync_respects_remote_rowid_checkpoint(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    _db(db)
    ingested: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "checkpoints":
            return httpx.Response(
                200,
                json={"status": "ready", "checkpoints": {"missions": {"last_cursor": "1"}}},
            )
        ingested.extend(body["records"])
        return httpx.Response(200, json={"status": "persisted", "accepted": len(body["records"])})

    result = await sync_critical_state(
        str(db),
        DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
        streams=("missions",),
        transport=httpx.MockTransport(handler),
    )

    assert result["mirrored"] == 0
    assert ingested == []


@pytest.mark.asyncio
async def test_sync_is_fail_closed_when_unconfigured(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    _db(db)
    result = await sync_critical_state(
        str(db),
        DurableMirrorConfig(url="", token="", enabled=True),
        streams=("missions",),
    )
    assert result == {"status": "disabled_or_unconfigured", "mirrored": 0, "streams": 0}
