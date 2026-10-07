from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import httpx
import pytest

from noema import durable_mirror
from noema.durable_mirror import (
    DEFAULT_STREAMS,
    MUTABLE_STREAMS,
    RESTORE_BUNDLE_FORMAT,
    RESTORE_BUNDLE_VERSION,
    DurableMirrorConfig,
    _canonical_hash,
    _decode_json_safe,
    _ensure_mutable_capture,
    _initialize_restore_schema,
    _json_safe,
    _legacy_canonical_hash,
    _load_restore_bundle,
    export_critical_state,
    restore_critical_state,
    sync_critical_state,
)


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


def test_mutable_stream_contract_matches_sql_and_edge_function() -> None:
    root = Path(__file__).resolve().parents[1]
    sql = (root / "supabase/migrations/20261006220000_versioned_mutable_mirror_and_export.sql").read_text()
    edge = (root / "supabase/functions/noema-durable-mirror/index.ts").read_text()
    config = (root / "supabase/config.toml").read_text()
    sql_block = re.search(r"if rec->>'stream'=any\(array\[(.*?)\]\) and", sql, re.DOTALL)
    edge_block = re.search(r"const mutableStreams = new Set\(\[(.*?)\]\);", edge, re.DOTALL)
    assert sql_block and edge_block
    sql_streams = set(re.findall(r"'([^']+)'", sql_block.group(1)))
    edge_streams = set(re.findall(r'"([^\"]+)"', edge_block.group(1)))
    assert sql_streams == edge_streams == set(MUTABLE_STREAMS)
    assert "source_rowid = split_part(record_key, ':', 2)::bigint" in sql
    assert "source_event_id = 'legacy:' || id::text" in sql
    assert "payload::text as payload_json" in sql
    assert "noema_mirror_export_manifest(p_streams text[])\nreturns jsonb language plpgsql stable" in sql
    assert "[functions.noema-durable-mirror]\nverify_jwt = false" in config


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
async def test_sync_ignores_legacy_mutable_rowid_checkpoint(tmp_path: Path) -> None:
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

    # A legacy rowid checkpoint cannot resume a change-event cursor safely.
    assert result["mirrored"] == 1
    assert len(ingested) == 1
    assert ingested[0]["operation"] == "upsert"
    assert ingested[0]["source_rowid"] == 1


@pytest.mark.asyncio
async def test_sync_rejects_mutable_checkpoint_ahead_of_local_journal(tmp_path: Path) -> None:
    db = tmp_path / "rolled-back-noema.db"
    _db(db)
    with sqlite3.connect(db) as conn:
        _, epoch = _ensure_mutable_capture(conn, "missions", 50)
        local_high_water = conn.execute(
            "SELECT coalesce(max(event_id),0) FROM noema_mirror_change_events WHERE stream='missions'",
        ).fetchone()[0]
        conn.commit()
    ingested: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "checkpoints":
            return httpx.Response(200, json={"checkpoints": {"missions": {
                "last_cursor": str(local_high_water + 20),
                "metadata": {"cursor_kind": "change_event_id", "capture_epoch": epoch},
            }}})
        ingested.extend(body["records"])
        return httpx.Response(200, json={"accepted": len(body["records"])})

    with pytest.raises(ValueError, match="ahead of the local source high-water mark"):
        await sync_critical_state(
            str(db), DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
            streams=("missions",), transport=httpx.MockTransport(handler),
        )
    assert ingested == []


@pytest.mark.asyncio
async def test_sync_rejects_append_only_checkpoint_ahead_of_local_high_water(tmp_path: Path) -> None:
    db = tmp_path / "rolled-back-append-only.db"
    _db(db)
    ingested: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "checkpoints":
            return httpx.Response(200, json={"checkpoints": {"economic_events": {
                "last_cursor": "21", "metadata": {"cursor_kind": "rowid"},
            }}})
        ingested.extend(body["records"])
        return httpx.Response(200, json={"accepted": len(body["records"])})

    with pytest.raises(ValueError, match="ahead of the local source high-water mark"):
        await sync_critical_state(
            str(db), DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
            streams=("economic_events",), transport=httpx.MockTransport(handler),
        )
    assert ingested == []


@pytest.mark.asyncio
async def test_sync_resumes_legacy_append_only_rowid_checkpoint(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    _db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO economic_events(id,provider,amount,created_at) VALUES (2,'openai','2.50','2026-10-06T16:02:00+00:00')",
        )
    ingested: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "checkpoints":
            return httpx.Response(200, json={
                "checkpoints": {"economic_events": {"last_cursor": "1"}},
            })
        ingested.extend(body["records"])
        return httpx.Response(200, json={"status": "persisted", "accepted": len(body["records"])})

    result = await sync_critical_state(
        str(db), DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
        streams=("economic_events",), transport=httpx.MockTransport(handler),
    )
    assert result["mirrored"] == 1
    assert [record["source_rowid"] for record in ingested] == [2]
    # Row 1 was already represented by the legacy checkpoint; only later rows
    # should be transmitted after the upgrade.


@pytest.mark.asyncio
async def test_sync_marks_append_only_write_after_page_fetch_as_lagging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "noema.db"
    _db(db)
    original_stream_rows = durable_mirror._stream_rows
    inserted = False

    def race_after_page(conn: sqlite3.Connection, stream: str, *, after_rowid: int, limit: int):
        nonlocal inserted
        rows = original_stream_rows(conn, stream, after_rowid=after_rowid, limit=limit)
        if not inserted:
            with sqlite3.connect(db) as writer:
                writer.execute(
                    "INSERT INTO economic_events(id,provider,amount,created_at) VALUES (2,'openai','2.50','2026-10-06T16:02:00+00:00')",
                )
            inserted = True
        return rows

    monkeypatch.setattr(durable_mirror, "_stream_rows", race_after_page)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "checkpoints":
            return httpx.Response(200, json={"checkpoints": {}})
        checkpoint = body["checkpoint"]
        assert checkpoint["last_cursor"] == "1"
        assert checkpoint["metadata"]["local_high_water"] == 2
        return httpx.Response(200, json={"accepted": len(body["records"])})

    result = await sync_critical_state(
        str(db), DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
        streams=("economic_events",), transport=httpx.MockTransport(handler),
    )
    assert result["lagging_streams"] == 1


def test_mutable_capture_recreates_triggers_after_schema_change(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    _db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO missions VALUES('m2','running','2026-10-06T16:02:00+00:00')")
        assert not _ensure_mutable_capture(conn, "missions", 1)[0]
        conn.execute("ALTER TABLE missions ADD COLUMN safety_note TEXT")
        assert not _ensure_mutable_capture(conn, "missions", 1)[0]
        baseline = json.loads(conn.execute(
            "SELECT payload_json FROM noema_mirror_change_events WHERE stream='missions' ORDER BY event_id DESC LIMIT 1",
        ).fetchone()[0])
        assert baseline["safety_note"] is None
        assert not _ensure_mutable_capture(conn, "missions", 1)[0]
        assert _ensure_mutable_capture(conn, "missions", 1)[0]
        conn.execute("UPDATE missions SET safety_note='reviewed' WHERE mission_id='m1'")
        payload = conn.execute(
            "SELECT payload_json FROM noema_mirror_change_events WHERE stream='missions' ORDER BY event_id DESC LIMIT 1",
        ).fetchone()[0]
    assert json.loads(payload)["safety_note"] == "reviewed"


def test_mutable_capture_supports_tables_without_timestamp_columns(tmp_path: Path) -> None:
    db = tmp_path / "noema.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE prediction_account_sync_state(venue TEXT, stream TEXT, cursor TEXT, PRIMARY KEY(venue,stream))")
        conn.execute("INSERT INTO prediction_account_sync_state VALUES('kalshi','fills','1')")
        _ensure_mutable_capture(conn, "prediction_account_sync_state", 100)
        conn.execute("UPDATE prediction_account_sync_state SET cursor='2' WHERE venue='kalshi'")
        row = conn.execute(
            "SELECT operation,occurred_at FROM noema_mirror_change_events ORDER BY event_id DESC LIMIT 1",
        ).fetchone()
    assert row == ("upsert", None)


def test_mirror_hash_normalizes_js_number_roundtrip_without_losing_large_values() -> None:
    from noema.durable_mirror import _canonical_hash

    assert _canonical_hash({"reliability": 0.0}) == _canonical_hash({"reliability": 0})
    value = {"reliability": 0.25, "exact_integer": 2**60}
    encoded = _json_safe(value)
    assert encoded["reliability"] == {"__noema_float_hex__": (0.25).hex()}
    assert encoded["exact_integer"] == {"__noema_integer__": str(2**60)}
    assert _decode_json_safe(encoded) == value


def test_legacy_hash_keeps_pre_normalization_float_encoding() -> None:
    payload = {"reliability": 0.0}
    assert _legacy_canonical_hash(payload) != _canonical_hash(payload)


@pytest.mark.asyncio
async def test_mutable_stream_captures_updates_and_deletions(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    _db(db)
    remote: list[dict[str, object]] = []
    checkpoints: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "checkpoints":
            return httpx.Response(200, json={"checkpoints": checkpoints})
        if body["action"] == "ingest":
            remote.extend(body["records"])
            cp = body.get("checkpoint")
            if cp:
                checkpoints[cp["stream"]] = {"last_cursor": cp["last_cursor"], "metadata": cp["metadata"]}
            return httpx.Response(200, json={"accepted": len(body["records"])})
        raise AssertionError(body)

    config = DurableMirrorConfig(url="https://mirror.test", token="t" * 40, rows_per_stream=50)
    transport = httpx.MockTransport(handler)
    await sync_critical_state(str(db), config, streams=("missions",), transport=transport)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE missions SET status='completed' WHERE mission_id='m1'")
        conn.commit()
    await sync_critical_state(str(db), config, streams=("missions",), transport=transport)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM missions WHERE mission_id='m1'")
        conn.commit()
    await sync_critical_state(str(db), config, streams=("missions",), transport=transport)

    assert [row["operation"] for row in remote] == ["upsert", "upsert", "tombstone"]
    assert len({row["record_key"] for row in remote}) == 1
    assert remote[0]["record_key"].startswith("missions:")
    assert remote[1]["payload"]["status"] == "completed"
    assert remote[2]["payload"]["__noema_tombstone__"] == 1


@pytest.mark.asyncio
async def test_export_writes_explicit_empty_streams_and_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "recovery.json"
    manifest_streams = [{
        "name": name, "record_count": 0, "restore_floor_id": 0,
        "checkpoint": {"last_cursor": "0", "metadata": {
            "cursor_kind": "change_event_id" if name in MUTABLE_STREAMS else "rowid",
            "local_high_water": 0, "baseline_complete": True,
            "restore_floor_id": 0, "capture_epoch": "epoch" if name in MUTABLE_STREAMS else None,
        }},
    } for name in DEFAULT_STREAMS]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "export_manifest":
            return httpx.Response(200, json={
                "complete": True, "through_cursor": 0, "streams": manifest_streams,
                "checkpoint_metadata": {"generated_at": "2026-10-06T00:00:00Z"},
            })
        if body["action"] == "pull":
            return httpx.Response(200, json={"records": [], "has_more": False, "next_id": 0})
        raise AssertionError(body)

    result = await export_critical_state(
        target, DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
        transport=httpx.MockTransport(handler),
    )
    assert result["status"] == "exported_complete"
    assert target.stat().st_mode & 0o777 == 0o600
    exported = json.loads(target.read_text(encoding="utf-8"))
    assert exported["format"] == RESTORE_BUNDLE_FORMAT
    assert exported["format_version"] == 2
    assert exported["complete"] is True
    assert len(exported["streams"]) == len(DEFAULT_STREAMS)
    assert all(stream["from_cursor"] == 0 and stream["records"] == [] for stream in exported["streams"])

    def incomplete(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"complete": False, "streams": []})

    with pytest.raises(RuntimeError, match="refused a complete export"):
        await export_critical_state(
            tmp_path / "should-not-exist.json",
            DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
            transport=httpx.MockTransport(incomplete),
        )
    assert not (tmp_path / "should-not-exist.json").exists()


@pytest.mark.asyncio
async def test_export_recovers_legacy_mirror_rows_with_original_sqlite_rowid(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    _restore_bundle(fixture)
    payload = next(item for item in json.loads(fixture.read_text())["streams"]
                   if item["name"] == "missions")["records"][0]["payload"]
    target = tmp_path / "legacy-export.json"
    manifest_streams = [{
        "name": name,
        "record_count": 1 if name == "missions" else 0,
        "restore_floor_id": 0,
        "checkpoint": {"last_cursor": "9", "metadata": {
            "cursor_kind": "change_event_id" if name in MUTABLE_STREAMS else "rowid",
            "local_high_water": 9, "baseline_complete": True,
            "restore_floor_id": 0, "capture_epoch": "new-epoch" if name in MUTABLE_STREAMS else None,
        }},
    } for name in DEFAULT_STREAMS]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "export_manifest":
            return httpx.Response(200, json={"complete": True, "through_cursor": 19,
                "streams": manifest_streams, "checkpoint_metadata": {}})
        if body["action"] == "pull":
            rows = [{"id": 19, "stream": "missions", "record_key": "missions:7",
                "version_sha256": _canonical_hash(payload), "occurred_at": payload["created_at"],
                "payload": payload, "operation": "upsert", "source_rowid": None,
                "source_event_id": None, "source_schema_version": None}] if body["streams"] == ["missions"] else []
            return httpx.Response(200, json={"records": rows, "has_more": False, "next_id": 19 if rows else 0})
        raise AssertionError(body)

    await export_critical_state(target, DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
        transport=httpx.MockTransport(handler))
    exported = json.loads(target.read_text())
    record = next(item for item in exported["streams"] if item["name"] == "missions")["records"][0]
    assert record["source_rowid"] == 7
    assert record["source_event_id"] == "legacy:19"
    assert record["source_schema_version"] == "legacy-unversioned"

    restored = tmp_path / "restored-legacy.db"
    restore_critical_state(target, restored)
    with sqlite3.connect(restored) as conn:
        row = conn.execute("SELECT rowid,mission_id,status FROM missions").fetchone()
        history = conn.execute("SELECT source_rowid,source_event_id,source_schema_version FROM noema_mirror_history WHERE stream='missions'").fetchone()
    assert row == (7, "mission-preserved", "completed")
    assert history == (7, "legacy:19", "legacy-unversioned")


@pytest.mark.asyncio
async def test_export_preserves_legacy_numeric_json_for_hash_validation(tmp_path: Path) -> None:
    original = tmp_path / "original.json"
    _restore_bundle(original)
    bundle = json.loads(original.read_text())
    legacy_payload = next(item for item in bundle["streams"] if item["name"] == "missions")["records"][0]["payload"]
    legacy_payload["legacy_real"] = 0.0
    digest = _legacy_canonical_hash(legacy_payload)
    target = tmp_path / "legacy-numeric-export.json"
    manifest_streams = [{
        "name": name,
        "record_count": 1 if name == "missions" else 0,
        "restore_floor_id": 0,
        "checkpoint": {"last_cursor": "9", "metadata": {
            "cursor_kind": "change_event_id" if name in MUTABLE_STREAMS else "rowid",
            "local_high_water": 9, "baseline_complete": True,
            "restore_floor_id": 0, "capture_epoch": "legacy-upgrade" if name in MUTABLE_STREAMS else None,
        }},
    } for name in DEFAULT_STREAMS]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "export_manifest":
            return httpx.Response(200, json={"complete": True, "through_cursor": 19,
                "streams": manifest_streams, "checkpoint_metadata": {}})
        if body["action"] == "pull":
            rows = [{"id": 19, "stream": "missions", "record_key": "missions:7",
                "version_sha256": digest, "occurred_at": legacy_payload["created_at"],
                # Model JSON.parse/JSON.stringify collapsing the object number.
                "payload": {**legacy_payload, "legacy_real": 0},
                "payload_json": json.dumps(legacy_payload, separators=(",", ":")),
                "operation": "upsert", "source_rowid": 7, "source_event_id": "legacy:19",
                "source_schema_version": "legacy-unversioned"}] if body["streams"] == ["missions"] else []
            return httpx.Response(200, json={"records": rows, "has_more": False, "next_id": 19 if rows else 0})
        raise AssertionError(body)

    await export_critical_state(target, DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
        transport=httpx.MockTransport(handler))
    exported = _load_restore_bundle(target)
    record = next(item for item in exported["streams"] if item["name"] == "missions")["records"][0]
    assert record["payload"]["legacy_real"] == 0.0


@pytest.mark.asyncio
async def test_export_refuses_aggregate_record_count_above_restore_limit(tmp_path: Path) -> None:
    target = tmp_path / "too-large.json"
    manifest_streams = [{
        "name": name,
        "record_count": 125_001 if name == "missions" else 125_000 if name == "economic_events" else 0,
        "checkpoint": {},
    } for name in DEFAULT_STREAMS]

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["action"] == "export_manifest"
        return httpx.Response(200, json={"complete": True, "through_cursor": 1,
            "streams": manifest_streams, "checkpoint_metadata": {}})

    with pytest.raises(RuntimeError, match="aggregate record limit"):
        await export_critical_state(target, DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
            transport=httpx.MockTransport(handler))
    assert not target.exists()


@pytest.mark.asyncio
async def test_export_refuses_bundle_above_restore_byte_limit(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "too-large.json"
    monkeypatch.setattr(durable_mirror, "MAX_RESTORE_BUNDLE_BYTES", 1000)
    manifest_streams = [{
        "name": name, "record_count": int(name == "economic_events"), "restore_floor_id": 0,
        "checkpoint": {"last_cursor": "0", "metadata": {
            "cursor_kind": "change_event_id" if name in MUTABLE_STREAMS else "rowid",
            "local_high_water": 0, "baseline_complete": True,
            "restore_floor_id": 0, "capture_epoch": "epoch" if name in MUTABLE_STREAMS else None,
        }},
    } for name in DEFAULT_STREAMS]
    payload = {"detail": "x" * 1500}
    record = {"id": 1, "stream": "economic_events", "record_key": "economic_events:1",
        "version_sha256": _canonical_hash(payload), "occurred_at": None, "operation": "upsert",
        "source_rowid": 1, "source_event_id": None, "source_schema_version": None,
        "source_commit": "fixture", "source_host": "fixture", "payload": payload}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["action"] == "export_manifest":
            return httpx.Response(200, json={"complete": True, "through_cursor": 1,
                "streams": manifest_streams, "checkpoint_metadata": {}})
        if body["action"] == "pull":
            rows = [record] if body["streams"] == ["economic_events"] else []
            return httpx.Response(200, json={"records": rows, "has_more": False})
        raise AssertionError(body)

    with pytest.raises(RuntimeError, match="bundle byte limit"):
        await export_critical_state(target, DurableMirrorConfig(url="https://mirror.test", token="t" * 40),
            transport=httpx.MockTransport(handler))
    assert not target.exists()


@pytest.mark.asyncio
async def test_mirror_export_restore_round_trip_versions_and_store_boot(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _initialize_restore_schema(source)
    with sqlite3.connect(source) as conn:
        for mission_id in ("m-live", "m-deleted"):
            conn.execute("""INSERT INTO missions(
                mission_id,trial_id,evidence_hash,objective,status,specialist,
                capability_grants_json,resource_grant_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                mission_id, "trial-" + mission_id, "a" * 64, "recovery drill", "running",
                "auditor", "[]", '{"live_execution":false}', "2026-10-06T00:00:00Z",
                "2026-10-06T00:00:00Z",
            ))

    records: list[dict[str, object]] = []
    checkpoints: dict[str, dict[str, object]] = {}
    next_id = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal next_id
        body = json.loads(request.content)
        if body["action"] == "checkpoints":
            return httpx.Response(200, json={"checkpoints": checkpoints})
        if body["action"] == "ingest":
            checkpoint = body.get("checkpoint")
            stream = checkpoint["stream"] if checkpoint else ""
            metadata = {**checkpoints.get(stream, {}).get("metadata", {}),
                        **(checkpoint["metadata"] if checkpoint else {})}
            if stream and stream not in checkpoints and stream in {
                "agent_runtime", "missions", "mission_handoffs", "ecosystem_specialists",
                "research_trials", "autonomous_research_runs", "bill_budget",
                "prediction_account_records", "prediction_account_sync_state",
            }:
                metadata = {**metadata, "restore_floor_id": next_id}
            for record in body["records"]:
                next_id += 1
                records.append({"id": next_id, **record})
            if checkpoint:
                checkpoints[stream] = {"last_cursor": checkpoint["last_cursor"], "metadata": metadata}
            return httpx.Response(200, json={"accepted": len(body["records"]), "duplicates": 0})
        if body["action"] == "export_manifest":
            streams = []
            for name in body["streams"]:
                cp = checkpoints.get(name, {"last_cursor": "0", "metadata": {
                    "cursor_kind": "change_event_id" if name in MUTABLE_STREAMS else "rowid",
                    "local_high_water": 0, "baseline_complete": True,
                    "restore_floor_id": 0, "capture_epoch": "epoch" if name in MUTABLE_STREAMS else None,
                }})
                rows = [row for row in records if row["stream"] == name]
                streams.append({"name": name, "record_count": len(rows),
                                "restore_floor_id": cp["metadata"].get("restore_floor_id", 0),
                                "checkpoint": cp})
            return httpx.Response(200, json={"complete": True, "through_cursor": next_id,
                "streams": streams, "checkpoint_metadata": {"test": True}})
        if body["action"] == "pull":
            rows = [row for row in records if row["stream"] in body["streams"]
                    and row["id"] > body["since_id"] and row["id"] <= body["through_id"]]
            rows = rows[:body["limit"]]
            return httpx.Response(200, json={"records": rows, "has_more": False,
                                              "next_id": rows[-1]["id"] if rows else body["since_id"]})
        raise AssertionError(body)

    config = DurableMirrorConfig(url="https://mirror.test", token="t" * 40, rows_per_stream=50)
    transport = httpx.MockTransport(handler)
    for _ in range(1):
        await sync_critical_state(str(source), config, streams=("missions",), transport=transport)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE missions SET status='completed',updated_at='2026-10-06T01:00:00Z' WHERE mission_id='m-live'")
        conn.execute("DELETE FROM missions WHERE mission_id='m-deleted'")
        conn.commit()
    await sync_critical_state(str(source), config, streams=("missions",), transport=transport)

    bundle = tmp_path / "complete.json"
    await export_critical_state(bundle, config, transport=transport)
    restored = tmp_path / "restored.db"
    report = restore_critical_state(bundle, restored)
    assert report["integrity_check"] == "ok"
    assert report["records_by_stream"]["missions"] == 1
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT mission_id,status FROM missions").fetchall() == [("m-live", "completed")]
        assert conn.execute("SELECT count(*) FROM noema_mirror_history WHERE stream='missions'").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM noema_mirror_history WHERE stream='missions' AND operation='tombstone'").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    from noema.agent_store import AgentStore
    from noema.autonomous_research import ResearchWorkStore
    from noema.bill_tracker import BillTracker
    from noema.economic_ledger import EconomicLedger
    from noema.ecosystem_store import EcosystemStore
    from noema.ledger import ForecastLedger
    from noema.mission_store import MissionStore
    from noema.research_trials import ResearchTrialStore
    stores = (
        AgentStore, EconomicLedger, EcosystemStore, ForecastLedger, MissionStore,
        ResearchTrialStore, ResearchWorkStore, BillTracker,
    )
    opened = []
    try:
        for store_type in stores:
            store = store_type(str(restored))
            opened.append(store)
        assert all(getattr(store, "conn", None) is not None for store in opened)
    finally:
        for store in opened:
            store.conn.close()


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


def _restore_bundle(path: Path) -> None:
    payload = {
        "mission_id": "mission-preserved",
        "trial_id": "trial-1",
        "evidence_hash": "e" * 64,
        "session_id": None,
        "run_id": None,
        "objective": "verify recoverable evidence",
        "status": "completed",
        "specialist": "auditor",
        "capability_grants_json": '["read_evidence_digest"]',
        "resource_grant_json": '{"live_execution":false}',
        "result_json": '{"verified":true}',
        "lesson_id": None,
        "created_at": "2026-10-06T16:00:00+00:00",
        "updated_at": "2026-10-06T16:10:00+00:00",
        "completed_at": "2026-10-06T16:10:00+00:00",
    }
    streams = []
    for name in DEFAULT_STREAMS:
        records = []
        through = 7
        if name == "missions":
            records = [{
                "mirror_id": 7,
                "stream": name,
                "record_key": "missions:5b226d697373696f6e2d707265736572766564225d",
                "version_sha256": _canonical_hash(payload),
                "occurred_at": payload["created_at"],
                "operation": "upsert",
                "source_rowid": 7,
                "source_event_id": "epoch:1",
                "payload": payload,
                "source_commit": "fixture",
                "source_host": "fixture",
            }]
        streams.append({
            "name": name,
            "from_cursor": 0,
            "through_cursor": through,
            "complete": True,
            "record_count": len(records),
            "restore_floor_id": 0,
            "checkpoint": {"last_cursor": str(through), "metadata": {
                "cursor_kind": "change_event_id" if name in MUTABLE_STREAMS else "rowid",
                "baseline_complete": True, "local_high_water": through,
                **({"restore_floor_id": 0, "capture_epoch": "fixture-epoch"}
                   if name in MUTABLE_STREAMS else {}),
            }},
            "records": records,
        })
    path.write_text(json.dumps({
        "format": RESTORE_BUNDLE_FORMAT,
        "format_version": RESTORE_BUNDLE_VERSION,
        "complete": True,
        "through_cursor": 7,
        "checkpoint_metadata": {},
        "streams": streams,
    }), encoding="utf-8")


@pytest.mark.parametrize("tamper", ["above_watermark", "negative", "checkpoint_mismatch"])
def test_restore_rejects_invalid_mutable_restore_floor(tmp_path: Path, tamper: str) -> None:
    bundle_path = tmp_path / "recovery.json"
    _restore_bundle(bundle_path)
    bundle = json.loads(bundle_path.read_text())
    stream = next(item for item in bundle["streams"] if item["name"] == "missions")
    if tamper == "above_watermark":
        stream["restore_floor_id"] = bundle["through_cursor"] + 1
        stream["checkpoint"]["metadata"]["restore_floor_id"] = stream["restore_floor_id"]
    elif tamper == "negative":
        stream["restore_floor_id"] = -1
        stream["checkpoint"]["metadata"]["restore_floor_id"] = -1
    else:
        stream["restore_floor_id"] = 3
        stream["checkpoint"]["metadata"]["restore_floor_id"] = 4
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="restore floor"):
        _load_restore_bundle(bundle_path)


def test_restore_complete_bundle_preserves_rowid_and_verifies_database(tmp_path: Path) -> None:
    bundle = tmp_path / "mirror-export.json"
    target = tmp_path / "recovered.db"
    _restore_bundle(bundle)

    report = restore_critical_state(bundle, target)

    assert report["status"] == "restored_and_verified"
    assert report["records"] == 1
    assert report["records_by_stream"]["missions"] == 1
    assert target.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT rowid,mission_id,status FROM missions"
        ).fetchone()
    assert row == (7, "mission-preserved", "completed")


def test_restore_verifies_append_only_payload_after_additive_schema_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "schema-evolved-export.json"
    target = tmp_path / "schema-evolved-restore.db"
    _restore_bundle(bundle_path)
    bundle = json.loads(bundle_path.read_text())
    stream = next(item for item in bundle["streams"] if item["name"] == "economic_events")
    payload = {
        "id": 4,
        "created_at": "2026-10-06T16:00:00+00:00",
        "event_type": "research_evidence",
        "amount_usd": None,
        "payload_json": "{}",
    }
    stream["records"] = [{
        "mirror_id": 7,
        "stream": "economic_events",
        "record_key": "economic_events:4",
        "version_sha256": _canonical_hash(payload),
        "occurred_at": payload["created_at"],
        "operation": "upsert",
        "source_rowid": 4,
        "source_event_id": None,
        "source_schema_version": None,
        "payload": payload,
    }]
    stream["record_count"] = 1
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    initialize = durable_mirror._initialize_restore_schema

    def initialize_with_new_optional_column(path: Path) -> None:
        initialize(path)
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE economic_events ADD COLUMN future_metadata TEXT DEFAULT 'introduced-later'")

    monkeypatch.setattr(durable_mirror, "_initialize_restore_schema", initialize_with_new_optional_column)
    report = restore_critical_state(bundle_path, target)

    assert report["status"] == "restored_and_verified"
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            "SELECT id,event_type,future_metadata FROM economic_events WHERE id=4",
        ).fetchone()
    assert row == (4, "research_evidence", "introduced-later")


def test_restore_refuses_partial_bundle_and_does_not_create_target(tmp_path: Path) -> None:
    bundle = tmp_path / "partial.json"
    target = tmp_path / "must-not-exist.db"
    _restore_bundle(bundle)
    data = json.loads(bundle.read_text(encoding="utf-8"))
    next(item for item in data["streams"] if item["name"] == "missions")["complete"] = False
    bundle.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="partial"):
        restore_critical_state(bundle, target)
    assert not target.exists()


def test_restore_rejects_hash_tampering_and_existing_target(tmp_path: Path) -> None:
    bundle = tmp_path / "tampered.json"
    target = tmp_path / "existing.db"
    _restore_bundle(bundle)
    data = json.loads(bundle.read_text(encoding="utf-8"))
    next(item for item in data["streams"] if item["name"] == "missions")["records"][0]["payload"]["status"] = "running"
    bundle.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        restore_critical_state(bundle, target)
    assert not target.exists()

    target.write_bytes(b"local truth")
    _restore_bundle(bundle)
    with pytest.raises(FileExistsError, match="never overwrites"):
        restore_critical_state(bundle, target)
    assert target.read_bytes() == b"local truth"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("operation", "tombstone", "tombstone identity"),
     ("record_key", "missions:deadbeef", "record key does not match")],
)
def test_restore_rejects_projection_metadata_tampering(tmp_path: Path, field: str, value: str, message: str) -> None:
    bundle, target = tmp_path / f"tampered-{field}.json", tmp_path / "must-not-exist.db"
    _restore_bundle(bundle)
    data = json.loads(bundle.read_text(encoding="utf-8"))
    record = next(item for item in data["streams"] if item["name"] == "missions")["records"][0]
    record[field] = value
    bundle.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        restore_critical_state(bundle, target)
    assert not target.exists()
