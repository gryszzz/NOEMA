from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import platform
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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

RESTORE_BUNDLE_FORMAT = "noema-durable-mirror-export"
RESTORE_BUNDLE_VERSION = 2
STREAMING_RESTORE_BUNDLE_VERSION = 3
MAX_RESTORE_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_RESTORE_RECORDS = 250_000
MAX_MIRROR_INGEST_BATCH_BYTES = 4_000_000

# Tables whose existing rows can change in place. Their durable identity is the
# declared primary key; rowid is retained separately only for SQLite recovery.
MUTABLE_STREAMS: dict[str, tuple[str, ...]] = {
    "agent_runtime": ("agent_id",),
    "agent_cycle_timings": ("cycle_id",),
    "missions": ("mission_id",),
    "mission_handoffs": ("handoff_id",),
    "ecosystem_specialists": ("name",),
    "research_trials": ("trial_id",),
    "autonomous_research_runs": ("id",),
    "bill_budget": ("id",),
    "prediction_account_records": ("venue", "record_type", "external_id"),
    "prediction_account_sync_state": ("venue", "stream"),
}
APPEND_ONLY_STREAMS = tuple(name for name in DEFAULT_STREAMS if name not in MUTABLE_STREAMS)


@dataclass(frozen=True)
class DurableMirrorConfig:
    url: str
    token: str
    enabled: bool = True
    timeout_seconds: float = 10.0
    rows_per_stream: int = 200

    @classmethod
    def from_env(cls) -> DurableMirrorConfig:
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
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > (2**53 - 1):
        return {"__noema_integer__": str(value)}
    if isinstance(value, float):
        if value.is_integer() and abs(value) <= (2**53 - 1):
            return int(value)
        return {"__noema_float_hex__": value.hex()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _json_safe(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _restore_projection_hash(record: dict[str, Any]) -> str:
    """Bind mirror identity and projection metadata to the verified payload digest."""
    return _canonical_hash({key: record.get(key) for key in (
        "mirror_id", "stream", "record_key", "version_sha256", "operation",
        "source_rowid", "source_event_id", "source_schema_version",
    )})


def _legacy_json_safe(value: Any) -> Any:
    """JSON encoding used by pre-normalization mirror writers."""
    if isinstance(value, bytes):
        return {"__noema_bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _legacy_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_legacy_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _legacy_canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _legacy_json_safe(payload), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str, allow_nan=False,
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
        key = _record_key(conn, stream, row)
        result.append({
            "stream": stream,
            "record_key": key,
            "version_sha256": _canonical_hash(payload),
            "occurred_at": _occurred_at(payload),
            "payload": payload,
            "source_commit": _source_commit(),
            "source_host": _source_host(),
            "source_rowid": rowid,
        })
    return result


def _source_projection_hash(record: dict[str, Any]) -> str:
    """Bind source identity to the payload before the mirror assigns its own cursor."""
    return _canonical_hash({key: record.get(key) for key in (
        "stream", "record_key", "version_sha256", "operation", "source_rowid",
        "source_event_id", "source_schema_version",
    )})


def _restore_record_from_mirror(row: dict[str, Any], stream: str) -> dict[str, Any]:
    """Normalize one mirror row for either recovery bundle format."""
    mirror_id = int(row.get("id") or 0)
    record_key = row.get("record_key")
    source_rowid = row.get("source_rowid")
    source_event_id = row.get("source_event_id")
    source_schema_version = row.get("source_schema_version")
    source_projection_sha256 = row.get("source_projection_sha256")
    payload = row.get("payload")
    payload_json = row.get("payload_json")
    if isinstance(payload_json, str):
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"durable mirror returned invalid payload JSON for {stream}") from exc
    legacy_identity = False
    if source_rowid is None and isinstance(record_key, str):
        prefix = f"{stream}:"
        suffix = record_key[len(prefix):] if record_key.startswith(prefix) else ""
        if suffix.isascii() and suffix.isdecimal() and int(suffix) > 0:
            source_rowid = int(suffix)
            legacy_identity = True
    if source_schema_version is None and source_projection_sha256 is None:
        source_schema_version = "legacy-unversioned"
    if stream in MUTABLE_STREAMS and source_event_id is None and legacy_identity:
        source_event_id = f"legacy:{mirror_id}"
        source_schema_version = source_schema_version or "legacy-unversioned"
    record = {
        "mirror_id": mirror_id, "stream": stream, "record_key": record_key,
        "version_sha256": row.get("version_sha256"), "occurred_at": row.get("occurred_at"),
        "operation": row.get("operation") or "upsert", "source_rowid": source_rowid,
        "source_event_id": source_event_id, "payload": payload,
        "source_commit": row.get("source_commit"), "source_host": row.get("source_host"),
        "source_schema_version": source_schema_version,
        "source_projection_sha256": source_projection_sha256,
    }
    record["projection_sha256"] = _restore_projection_hash(record)
    return record


def _primary_key_columns(conn: sqlite3.Connection, stream: str) -> tuple[str, ...]:
    quoted = '"' + stream.replace('"', '""') + '"'
    columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
    return tuple(str(row[1]) for row in sorted(columns, key=lambda row: int(row[5]) or 10**9) if int(row[5]))


def _validate_restore_projection_identity(
    conn: sqlite3.Connection, stream: str, record: dict[str, Any],
) -> None:
    """Bind the projected row key and operation to verified payload identity."""
    key = record["record_key"]
    suffix = key[len(stream) + 1:]
    payload = record["payload"]
    operation = record.get("operation", "upsert")
    if (record.get("source_schema_version") == "legacy-unversioned"
            and operation == "upsert" and suffix.isascii() and suffix.isdecimal()
            and int(suffix) == record.get("source_rowid")
            and "__noema_tombstone__" not in payload):
        # Original append-only mirror rows keyed by source rowid predate stable
        # primary-key identities. Preserve that specific legacy representation.
        return
    columns = _primary_key_columns(conn, stream)
    if not columns:
        raise ValueError(f"restore stream {stream} has no primary-key identity")
    if operation == "tombstone":
        identity = payload.get("identity")
        if isinstance(identity, str):
            try:
                identity = json.loads(identity)
            except json.JSONDecodeError:
                identity = None
        if (set(payload) != {"__noema_tombstone__", "identity"}
                or payload.get("__noema_tombstone__") != 1):
            raise ValueError(f"restore tombstone identity is invalid in {stream}")
        if isinstance(identity, dict) and set(identity) == set(columns):
            values = [identity[column] for column in columns]
        elif isinstance(identity, list) and len(identity) == len(columns):
            values = identity
        else:
            raise ValueError(f"restore tombstone identity is invalid in {stream}")
    else:
        if "__noema_tombstone__" in payload or any(column not in payload for column in columns):
            raise ValueError(f"restore upsert identity is invalid in {stream}")
        values = [payload[column] for column in columns]
        if (len(columns) == 1 and suffix.isascii() and suffix.isdecimal()
                and str(values[0]) == suffix and record.get("source_rowid") == int(suffix)):
            # Older append-only writers encoded a single numeric primary key
            # directly in record_key instead of as SQLite JSON-array hex.
            return
    placeholders = ",".join("?" for _ in values)
    encoded = conn.execute(
        f"SELECT lower(hex(CAST(json_array({placeholders}) AS BLOB)))", tuple(values),
    ).fetchone()[0]
    if suffix != encoded:
        raise ValueError(f"restore record key does not match payload identity in {stream}")


def _record_key(conn: sqlite3.Connection, stream: str, row: dict[str, Any]) -> str:
    keys = _primary_key_columns(conn, stream)
    if not keys or any(row.get(key) is None for key in keys):
        raise ValueError(f"critical stream {stream} has no stable non-null primary key")
    # Ask SQLite to encode the values so triggers and the worker use the same
    # canonical JSON representation regardless of Python's JSON formatting.
    encoded = conn.execute(
        "SELECT hex(CAST(json_array(" + ",".join("?" for _ in keys) + ") AS BLOB))",
        tuple(row[key] for key in keys),
    ).fetchone()[0]
    return f"{stream}:{str(encoded).lower()}"


def _qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ensure_mutable_capture(conn: sqlite3.Connection, stream: str, limit: int) -> tuple[bool, str]:
    """Install local CDC triggers and incrementally seed the durable baseline."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    keys = MUTABLE_STREAMS[stream]
    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_qident(stream)})")]
    missing = set(keys) - set(columns)
    if missing:
        raise ValueError(f"critical stream {stream} lacks stable key columns")
    conn.execute("""CREATE TABLE IF NOT EXISTS noema_mirror_change_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL,
        record_key TEXT NOT NULL, source_rowid INTEGER, operation TEXT NOT NULL,
        payload_json TEXT NOT NULL, occurred_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS noema_mirror_capture_state (
        stream TEXT PRIMARY KEY, seed_rowid INTEGER NOT NULL DEFAULT 0,
        baseline_complete INTEGER NOT NULL DEFAULT 0, capture_epoch TEXT NOT NULL,
        schema_signature TEXT
    )""")
    try:
        conn.execute("ALTER TABLE noema_mirror_capture_state ADD COLUMN capture_epoch TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE noema_mirror_capture_state ADD COLUMN schema_signature TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "INSERT OR IGNORE INTO noema_mirror_capture_state(stream,capture_epoch) VALUES (?,?)",
        (stream, str(uuid.uuid4())),
    )
    conn.execute("UPDATE noema_mirror_capture_state SET capture_epoch=? WHERE stream=? AND capture_epoch IS NULL", (str(uuid.uuid4()), stream))

    names_new = ",".join(_sql_string(name) + ",NEW." + _qident(name) for name in columns)
    new_key = "hex(CAST(json_array(" + ",".join("NEW." + _qident(k) for k in keys) + ") AS BLOB))"
    old_key = "hex(CAST(json_array(" + ",".join("OLD." + _qident(k) for k in keys) + ") AS BLOB))"
    new_record = _sql_string(stream + ":") + " || lower(" + new_key + ")"
    old_record = _sql_string(stream + ":") + " || lower(" + old_key + ")"
    payload_new = "json_object(" + names_new + ")"
    identity_old = "json_object(" + ",".join(_sql_string(k) + ",OLD." + _qident(k) for k in keys) + ")"
    ts_new = "coalesce(" + ",".join("NEW." + _qident(c) for c in _TIMESTAMP_FIELDS if c in columns) + ("," if any(c in columns for c in _TIMESTAMP_FIELDS) else "") + "NULL)"
    ts_old = "coalesce(" + ",".join("OLD." + _qident(c) for c in _TIMESTAMP_FIELDS if c in columns) + ("," if any(c in columns for c in _TIMESTAMP_FIELDS) else "") + "NULL)"
    prefix = "noema_mirror_" + stream
    schema_signature = _canonical_hash({"columns": [tuple(row) for row in conn.execute(
        f"PRAGMA table_info({_qident(stream)})",
    )]})
    timestamp_new = ts_new if any(c in columns for c in _TIMESTAMP_FIELDS) else "NULL"
    timestamp_old = ts_old if any(c in columns for c in _TIMESTAMP_FIELDS) else "NULL"
    trigger_definitions = (
      f"""CREATE TRIGGER {_qident(prefix + '_ai')} AFTER INSERT ON {_qident(stream)} BEGIN
        INSERT INTO noema_mirror_change_events(stream,record_key,source_rowid,operation,payload_json,occurred_at)
        VALUES ({_sql_string(stream)},{new_record},NEW.rowid,'upsert',{payload_new},{timestamp_new});
      END""",
      f"""CREATE TRIGGER {_qident(prefix + '_ad')} AFTER DELETE ON {_qident(stream)} BEGIN
        INSERT INTO noema_mirror_change_events(stream,record_key,source_rowid,operation,payload_json,occurred_at)
        VALUES ({_sql_string(stream)},{old_record},OLD.rowid,'tombstone',json_object('__noema_tombstone__',1,'identity',{identity_old}),{timestamp_old});
      END""",
      f"""CREATE TRIGGER {_qident(prefix + '_au')} AFTER UPDATE ON {_qident(stream)} BEGIN
        INSERT INTO noema_mirror_change_events(stream,record_key,source_rowid,operation,payload_json,occurred_at)
        SELECT {_sql_string(stream)},{old_record},OLD.rowid,'tombstone',json_object('__noema_tombstone__',1,'identity',{identity_old}),{timestamp_old}
          WHERE {old_record} <> {new_record};
        INSERT INTO noema_mirror_change_events(stream,record_key,source_rowid,operation,payload_json,occurred_at)
        VALUES ({_sql_string(stream)},{new_record},NEW.rowid,'upsert',{payload_new},{timestamp_new});
      END""",
    )

    state = conn.execute("SELECT seed_rowid,baseline_complete,schema_signature FROM noema_mirror_capture_state WHERE stream=?", (stream,)).fetchone()
    capture_epoch = str(conn.execute("SELECT capture_epoch FROM noema_mirror_capture_state WHERE stream=?", (stream,)).fetchone()[0])
    if state[2] != schema_signature:
        for suffix in ("_ai", "_ad", "_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {_qident(prefix + suffix)}")
        for definition in trigger_definitions:
            conn.execute(definition)
        conn.execute("""UPDATE noema_mirror_capture_state
            SET schema_signature=?,seed_rowid=0,baseline_complete=0 WHERE stream=?""",
            (schema_signature, stream))
        state = conn.execute(
            "SELECT seed_rowid,baseline_complete,schema_signature FROM noema_mirror_capture_state WHERE stream=?",
            (stream,),
        ).fetchone()
    if int(state[1]):
        return True, capture_epoch
    quoted = _qident(stream)
    cursor = conn.execute(
        f"SELECT rowid AS _source_rowid,* FROM {quoted} WHERE rowid>? ORDER BY rowid LIMIT ?",
        (int(state[0]), limit),
    )
    rows = cursor.fetchall()
    col_names = [item[0] for item in cursor.description]
    last = int(state[0])
    for raw in rows:
        row = dict(zip(col_names, raw))
        rowid = int(row.pop("_source_rowid"))
        # Match SQLite json_array's UTF-8 output (the key bytes are not payload hashes).
        encoded = conn.execute("SELECT lower(hex(CAST(json_array(" + ",".join("?" for _ in keys) + ") AS BLOB)))", tuple(row[name] for name in keys)).fetchone()[0]
        key = f"{stream}:{encoded}"
        payload = json.dumps(_json_safe(row), ensure_ascii=False, separators=(",", ":"))
        occurred = _occurred_at(row)
        conn.execute("INSERT INTO noema_mirror_change_events(stream,record_key,source_rowid,operation,payload_json,occurred_at) VALUES(?,?,?,'upsert',?,?)", (stream, key, rowid, payload, occurred))
        last = rowid
    done = len(rows) < limit
    conn.execute("UPDATE noema_mirror_capture_state SET seed_rowid=?,baseline_complete=? WHERE stream=?", (last, int(done), stream))
    return done, capture_epoch


@contextmanager
def mutable_schema_migration(
    conn: sqlite3.Connection,
    stream: str,
    rows_per_stream: int = 1000,
) -> Iterator[None]:
    """Temporarily remove CDC triggers around mutable-table DDL, then reseed and reinstall them."""
    if stream not in MUTABLE_STREAMS:
        raise ValueError(f"{stream} is not a mutable mirror stream")
    prefix = "noema_mirror_" + stream
    with conn:
        for suffix in ("_ai", "_ad", "_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {_qident(prefix + suffix)}")
        conn.execute(
            "UPDATE noema_mirror_capture_state SET schema_signature='' WHERE stream=?",
            (stream,),
        )
        try:
            yield
        finally:
            _ensure_mutable_capture(conn, stream, rows_per_stream)


def _mutable_events(
    conn: sqlite3.Connection, stream: str, cursor: int, limit: int, capture_epoch: str,
) -> list[dict[str, Any]]:
    events = conn.execute(
        "SELECT event_id,record_key,source_rowid,operation,payload_json,occurred_at FROM noema_mirror_change_events WHERE stream=? AND event_id>? ORDER BY event_id LIMIT ?",
        (stream, cursor, limit),
    ).fetchall()
    return [{
        "stream": stream, "record_key": str(event[1]), "version_sha256": _canonical_hash(json.loads(event[4])),
        "occurred_at": event[5], "payload": _json_safe(json.loads(event[4])), "operation": event[3],
        "source_rowid": int(event[2]) if event[2] is not None else None,
        "source_event_id": f"{capture_epoch}:{int(event[0])}",
        "source_commit": _source_commit(), "source_host": _source_host(), "_cursor": int(event[0]),
    } for event in events]


def _cursor_fingerprint(
    conn: sqlite3.Connection, stream: str, cursor: int, *, mutable: bool,
) -> str:
    """Hash the complete local source prefix represented by a remote cursor."""
    digest = hashlib.sha256()
    if mutable:
        rows = conn.execute(
            "SELECT event_id,record_key,source_rowid,operation,payload_json,occurred_at "
            "FROM noema_mirror_change_events WHERE stream=? AND event_id<=? ORDER BY event_id",
            (stream, cursor),
        )
        for event_id, record_key, source_rowid, operation, payload_json, occurred_at in rows:
            entry = {
                "event_id": int(event_id), "record_key": str(record_key),
                "source_rowid": int(source_rowid) if source_rowid is not None else None,
                "operation": str(operation), "payload": _json_safe(json.loads(payload_json)),
                "occurred_at": occurred_at,
            }
            encoded = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    else:
        rows = conn.execute(
            f"SELECT rowid AS _cursor_rowid,* FROM {_qident(stream)} WHERE rowid<=? ORDER BY rowid",
            (cursor,),
        )
        names = [column[0] for column in rows.description]
        for raw in rows:
            record = dict(zip(names, raw))
            rowid = int(record.pop("_cursor_rowid"))
            entry = {"rowid": rowid, "payload": _json_safe(record)}
            encoded = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _cursor_fingerprint_v2(
    conn: sqlite3.Connection, stream: str, cursor: int, *, mutable: bool,
) -> str:
    """Build a versioned rolling digest for a source prefix (migration fallback)."""
    digest = hashlib.sha256(b"NOEMA durable mirror cursor fingerprint v2").digest()
    return _advance_cursor_fingerprint_v2(
        conn, stream, 0, cursor, digest.hex(), mutable=mutable,
    )


def _advance_cursor_fingerprint_v2(
    conn: sqlite3.Connection, stream: str, after_cursor: int, through_cursor: int,
    previous_fingerprint: str, *, mutable: bool,
) -> str:
    """Extend a rolling source digest using only rows after the saved cursor."""
    try:
        digest = bytes.fromhex(previous_fingerprint)
    except ValueError as exc:
        raise ValueError("invalid local cursor fingerprint") from exc
    if len(digest) != hashlib.sha256().digest_size:
        raise ValueError("invalid local cursor fingerprint")
    if mutable:
        rows = conn.execute(
            "SELECT event_id,record_key,source_rowid,operation,payload_json,occurred_at "
            "FROM noema_mirror_change_events WHERE stream=? AND event_id>? AND event_id<=? ORDER BY event_id",
            (stream, after_cursor, through_cursor),
        )
        entries = ({
            "event_id": int(event_id), "record_key": str(record_key),
            "source_rowid": int(source_rowid) if source_rowid is not None else None,
            "operation": str(operation), "payload": _json_safe(json.loads(payload_json)),
            "occurred_at": occurred_at,
        } for event_id, record_key, source_rowid, operation, payload_json, occurred_at in rows)
    else:
        rows = conn.execute(
            f"SELECT rowid AS _cursor_rowid,* FROM {_qident(stream)} "
            "WHERE rowid>? AND rowid<=? ORDER BY rowid",
            (after_cursor, through_cursor),
        )
        names = [column[0] for column in rows.description]
        def append_only_entries():
            for raw in rows:
                record = dict(zip(names, raw))
                rowid = int(record.pop("_cursor_rowid"))
                yield {"rowid": rowid, "payload": _json_safe(record)}
        entries = append_only_entries()
    for entry in entries:
        encoded = json.dumps(
            entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        digest = hashlib.sha256(digest + len(encoded).to_bytes(8, "big") + encoded).digest()
    return digest.hex()


def _ensure_cursor_state_table(conn: sqlite3.Connection, streams: tuple[str, ...]) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS noema_mirror_change_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL,
        record_key TEXT NOT NULL, source_rowid INTEGER, operation TEXT NOT NULL,
        payload_json TEXT NOT NULL, occurred_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS noema_mirror_cursor_state (
        stream TEXT PRIMARY KEY, cursor_kind TEXT NOT NULL, capture_epoch TEXT NOT NULL DEFAULT '',
        last_cursor INTEGER NOT NULL, fingerprint_version INTEGER NOT NULL,
        fingerprint_sha256 TEXT NOT NULL
    )""")
    for stream in streams:
        quoted_stream = _qident(stream)
        literal_stream = _sql_string(stream)
        for operation in ("UPDATE", "DELETE"):
            suffix = "au" if operation == "UPDATE" else "ad"
            trigger_name = _qident("noema_mirror_cursor_invalidate_" + stream + "_" + suffix)
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {trigger_name}
                AFTER {operation} ON {quoted_stream} BEGIN
                  DELETE FROM noema_mirror_cursor_state
                  WHERE stream={literal_stream} AND last_cursor>=OLD.rowid;
                END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS noema_mirror_cursor_event_invalidate_au
        AFTER UPDATE ON noema_mirror_change_events BEGIN
          DELETE FROM noema_mirror_cursor_state
          WHERE stream=OLD.stream AND last_cursor>=OLD.event_id;
        END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS noema_mirror_cursor_event_invalidate_ad
        AFTER DELETE ON noema_mirror_change_events BEGIN
          DELETE FROM noema_mirror_cursor_state
          WHERE stream=OLD.stream AND last_cursor>=OLD.event_id;
        END""")


def _fingerprint_at_remote_cursor(
    conn: sqlite3.Connection, stream: str, cursor: int, *, mutable: bool,
    capture_epoch: str, remote_meta: dict[str, Any],
) -> str:
    """Verify remote continuity with a saved local digest, hashing history only on cache misses."""
    cursor_kind = "change_event_id" if mutable else "rowid"
    state = conn.execute(
        "SELECT cursor_kind,capture_epoch,last_cursor,fingerprint_version,fingerprint_sha256 "
        "FROM noema_mirror_cursor_state WHERE stream=?", (stream,),
    ).fetchone()
    local_fingerprint = None
    if (state is not None and state[0] == cursor_kind and state[1] == capture_epoch
            and int(state[2]) == cursor and int(state[3]) == 2):
        local_fingerprint = str(state[4])

    remote_fingerprint = remote_meta.get("cursor_fingerprint")
    remote_version = remote_meta.get("cursor_fingerprint_version", 1)
    if local_fingerprint is not None:
        if remote_fingerprint is not None and remote_version == 2 and remote_fingerprint != local_fingerprint:
            raise ValueError(
                f"remote checkpoint for {stream} does not match the local source cursor history"
            )
        if remote_fingerprint is not None and remote_version != 2:
            legacy_fingerprint = _cursor_fingerprint(conn, stream, cursor, mutable=mutable)
            if remote_fingerprint != legacy_fingerprint:
                raise ValueError(
                    f"remote checkpoint for {stream} does not match the local source cursor history"
                )
        return local_fingerprint

    if remote_fingerprint is not None and remote_version != 2:
        legacy_fingerprint = _cursor_fingerprint(conn, stream, cursor, mutable=mutable)
        if remote_fingerprint != legacy_fingerprint:
            raise ValueError(
                f"remote checkpoint for {stream} does not match the local source cursor history"
            )
    fingerprint = _cursor_fingerprint_v2(conn, stream, cursor, mutable=mutable)
    if remote_fingerprint is not None and remote_version == 2 and remote_fingerprint != fingerprint:
        raise ValueError(
            f"remote checkpoint for {stream} does not match the local source cursor history"
        )
    return fingerprint


def _save_cursor_fingerprint(
    conn: sqlite3.Connection, stream: str, cursor: int, *, mutable: bool,
    capture_epoch: str, fingerprint: str,
) -> None:
    conn.execute("""INSERT INTO noema_mirror_cursor_state
        (stream,cursor_kind,capture_epoch,last_cursor,fingerprint_version,fingerprint_sha256)
        VALUES (?,?,?,?,2,?) ON CONFLICT(stream) DO UPDATE SET
        cursor_kind=excluded.cursor_kind,capture_epoch=excluded.capture_epoch,
        last_cursor=excluded.last_cursor,fingerprint_version=excluded.fingerprint_version,
        fingerprint_sha256=excluded.fingerprint_sha256""",
        (stream, "change_event_id" if mutable else "rowid", capture_epoch, cursor, fingerprint))


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


async def export_critical_state(
    output_path: str | Path,
    config: DurableMirrorConfig | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Write a complete disk-backed recovery bundle without a lifetime row cap."""
    config = config or DurableMirrorConfig.from_env()
    if not config.ready:
        raise RuntimeError("durable mirror export is not configured")
    target = Path(output_path).expanduser().absolute()
    if target.exists():
        raise FileExistsError("export target already exists; choose a new recovery bundle path")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    conn = sqlite3.connect(temp)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE bundle_manifest (manifest_json TEXT NOT NULL)")
        conn.execute("""CREATE TABLE bundle_records (
            stream TEXT NOT NULL, mirror_id INTEGER NOT NULL, record_json TEXT NOT NULL,
            PRIMARY KEY(stream, mirror_id)
        )""")
        async with httpx.AsyncClient(timeout=config.timeout_seconds, transport=transport) as client:
            manifest = await _post(client, config, {
                "action": "export_manifest", "streams": list(DEFAULT_STREAMS),
            })
            if manifest.get("complete") is not True or manifest.get("streams") is None:
                raise RuntimeError("durable mirror refused a complete export; checkpoints or streams are incomplete")
            try:
                watermark = int(manifest.get("through_cursor"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("durable mirror export manifest has no valid watermark") from exc
            declared = {item.get("name"): item for item in manifest["streams"] if isinstance(item, dict)}
            if set(declared) != set(DEFAULT_STREAMS):
                raise RuntimeError("durable mirror export manifest omitted critical streams")
            stream_metadata: list[dict[str, Any]] = []
            total_records = 0
            for stream in DEFAULT_STREAMS:
                meta = declared[stream]
                try:
                    expected_count = int(meta["record_count"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(f"durable mirror manifest has no count for {stream}") from exc
                if expected_count < 0:
                    raise RuntimeError(f"durable mirror manifest has an invalid count for {stream}")
                checkpoint = meta.get("checkpoint")
                if not isinstance(checkpoint, dict):
                    raise TypeError(f"durable mirror omitted checkpoint metadata for {stream}")
                checkpoint_metadata = checkpoint.get("metadata")
                if not isinstance(checkpoint_metadata, dict) or checkpoint_metadata.get("baseline_complete") is not True:
                    raise RuntimeError(f"durable mirror checkpoint is incomplete for {stream}")
                expected_kind = "change_event_id" if stream in MUTABLE_STREAMS else "rowid"
                if checkpoint_metadata.get("cursor_kind") != expected_kind:
                    raise RuntimeError(f"durable mirror checkpoint cursor type is invalid for {stream}")
                try:
                    last_cursor = int(checkpoint.get("last_cursor") or 0)
                    local_high_water = int(checkpoint_metadata["local_high_water"])
                    floor = int(meta.get("restore_floor_id") or 0)
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(f"durable mirror checkpoint is invalid for {stream}") from exc
                if last_cursor != local_high_water:
                    raise RuntimeError(f"durable mirror checkpoint does not match local state for {stream}")
                if stream in MUTABLE_STREAMS:
                    try:
                        checkpoint_floor = int(checkpoint_metadata["restore_floor_id"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeError(f"mutable stream checkpoint has no restore floor for {stream}") from exc
                    if checkpoint_floor != floor or not checkpoint_metadata.get("capture_epoch"):
                        raise RuntimeError(f"mutable stream restore floor is inconsistent for {stream}")
                elif floor != 0:
                    raise RuntimeError(f"append-only stream {stream} cannot declare a mutable restore floor")
                since_id = 0
                seen = 0
                previous = 0
                while True:
                    page = await _post(client, config, {
                        "action": "pull", "streams": [stream], "since_id": since_id,
                        "through_id": watermark, "limit": min(1000, max(50, config.rows_per_stream * 5)),
                    })
                    rows = page.get("records")
                    if not isinstance(rows, list):
                        raise TypeError(f"durable mirror returned an invalid page for {stream}")
                    for row in rows:
                        if not isinstance(row, dict) or row.get("stream") != stream:
                            raise RuntimeError(f"durable mirror returned a malformed record for {stream}")
                        record = _restore_record_from_mirror(row, stream)
                        mirror_id = int(record["mirror_id"])
                        if mirror_id <= since_id or mirror_id > watermark:
                            raise RuntimeError(f"durable mirror returned unordered/out-of-watermark records for {stream}")
                        try:
                            previous = _validate_restore_record(
                                stream, record, previous, watermark, require_source_projection=True,
                            )
                        except (TypeError, ValueError) as exc:
                            raise RuntimeError(
                                "durable mirror export contains records that fail local restore validation"
                            ) from exc
                        conn.execute("INSERT INTO bundle_records VALUES(?,?,?)", (
                            stream, mirror_id,
                            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                        ))
                        since_id = mirror_id
                        seen += 1
                    if page.get("has_more") is not True:
                        break
                    if not rows:
                        raise RuntimeError(f"durable mirror returned an incomplete empty page for {stream}")
                if seen != expected_count:
                    raise RuntimeError(f"durable mirror export is incomplete for {stream}")
                total_records += seen
                stream_metadata.append({
                    "name": stream, "from_cursor": 0, "through_cursor": watermark,
                    "complete": True, "record_count": seen, "checkpoint": checkpoint,
                    "restore_floor_id": floor,
                })
                conn.commit()
        bundle_header = {
            "format": RESTORE_BUNDLE_FORMAT,
            "format_version": STREAMING_RESTORE_BUNDLE_VERSION,
            "complete": True, "through_cursor": watermark,
            "checkpoint_metadata": manifest.get("checkpoint_metadata", {}),
            "streams": stream_metadata,
        }
        conn.execute("INSERT INTO bundle_manifest VALUES(?)", (
            json.dumps(bundle_header, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        ))
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("durable mirror export bundle failed its SQLite integrity check")
        conn.close()
        os.chmod(temp, 0o600)
        fd = os.open(temp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(temp, target)
        temp.unlink()
        return {"status": "exported_complete", "output": str(target), "through_cursor": watermark,
                "streams": len(stream_metadata), "records": total_records,
                "format_version": STREAMING_RESTORE_BUNDLE_VERSION}
    except Exception:
        conn.close()
        temp.unlink(missing_ok=True)
        raise


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

    conn = sqlite3.connect(database, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        present = tuple(stream for stream in streams if stream in _tables(conn))
        if not present:
            return {"status": "no_streams", "mirrored": 0, "streams": 0}
        _ensure_cursor_state_table(
            conn, tuple(stream for stream in present if stream not in MUTABLE_STREAMS),
        )
        conn.commit()

        async with httpx.AsyncClient(
            timeout=config.timeout_seconds, transport=transport,
        ) as client:
            checkpoint_response = await _post(
                client, config, {"action": "checkpoints", "streams": list(present)},
            )
            checkpoints = checkpoint_response.get("checkpoints")
            if not isinstance(checkpoints, dict):
                checkpoints = {}

            capture_states: dict[str, tuple[bool, str]] = {}
            for stream in present:
                if stream in MUTABLE_STREAMS:
                    capture_states[stream] = _ensure_mutable_capture(
                        conn, stream, config.rows_per_stream,
                    )
                    conn.commit()

            # Validate every local stream and prepare every request before the
            # first remote write. A bad later stream must not leave an earlier
            # stream's remote checkpoint advanced. The immediate transaction
            # also keeps each row page and its fingerprint on one local snapshot.
            plans: list[dict[str, Any]] = []
            conn.execute("BEGIN IMMEDIATE")
            for stream in present:
                mutable = stream in MUTABLE_STREAMS
                baseline_complete, capture_epoch = capture_states.get(stream, (True, ""))
                raw_checkpoint = checkpoints.get(stream)
                cursor = 0
                expected_kind = "change_event_id" if mutable else "rowid"
                remote_meta = raw_checkpoint.get("metadata", {}) if isinstance(raw_checkpoint, dict) else {}
                if (isinstance(raw_checkpoint, dict) and remote_meta.get("cursor_kind") == expected_kind
                        and (not mutable or remote_meta.get("capture_epoch") == capture_epoch)):
                    try:
                        cursor = max(0, int(raw_checkpoint.get("last_cursor") or 0))
                    except (TypeError, ValueError):
                        cursor = 0
                elif (not mutable and isinstance(raw_checkpoint, dict)
                      and "cursor_kind" not in remote_meta):
                    # The parent worker stored append-only checkpoints as SQLite
                    # rowids without cursor metadata. Preserve that cursor during
                    # upgrade; mutable streams must still reseed under CDC.
                    try:
                        cursor = max(0, int(raw_checkpoint.get("last_cursor") or 0))
                    except (TypeError, ValueError):
                        cursor = 0

                rows = (_mutable_events(conn, stream, cursor, config.rows_per_stream, capture_epoch) if mutable else
                        _stream_rows(conn, stream, after_rowid=cursor, limit=config.rows_per_stream))
                last_cursor = cursor
                if rows:
                    last_cursor = int(rows[-1]["_cursor"] if mutable else rows[-1]["source_rowid"])
                payload_rows = [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]
                for record in payload_rows:
                    record["operation"] = record.get("operation") or "upsert"
                    record["source_event_id"] = record.get("source_event_id")
                    record["source_schema_version"] = "sqlite-v1"
                    record["source_projection_sha256"] = _source_projection_hash(record)
                if mutable:
                    local_high_water = int(conn.execute("SELECT coalesce(max(event_id),0) FROM noema_mirror_change_events WHERE stream=?", (stream,)).fetchone()[0])
                else:
                    local_high_water = int(conn.execute(f"SELECT coalesce(max(rowid),0) FROM {_qident(stream)}").fetchone()[0])
                if cursor > local_high_water:
                    raise ValueError(
                        f"remote checkpoint for {stream} is ahead of the local source high-water mark"
                    )
                cursor_fingerprint = _fingerprint_at_remote_cursor(
                    conn, stream, cursor, mutable=mutable, capture_epoch=capture_epoch or "",
                    remote_meta=remote_meta,
                )
                new_fingerprint = _advance_cursor_fingerprint_v2(
                    conn, stream, cursor, last_cursor, cursor_fingerprint, mutable=mutable,
                )
                checkpoint = {
                    "stream": stream,
                    "last_cursor": str(last_cursor),
                    "metadata": {
                        "source_commit": _source_commit(),
                        "source_host": _source_host(),
                        "cursor_kind": expected_kind,
                        "local_high_water": local_high_water,
                        "baseline_complete": baseline_complete,
                        "capture_epoch": capture_epoch or None,
                        "cursor_fingerprint_version": 2,
                        "cursor_fingerprint": new_fingerprint,
                    },
                }
                batches: list[list[dict[str, Any]]] = []
                batch: list[dict[str, Any]] = []
                for record in payload_rows:
                    candidate = [*batch, record]
                    envelope = {"action": "ingest", "records": candidate, "checkpoint": checkpoint}
                    encoded_size = len(json.dumps(
                        envelope, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8"))
                    if len(candidate) > 500 or encoded_size > MAX_MIRROR_INGEST_BATCH_BYTES:
                        if not batch:
                            raise ValueError(
                                f"single durable mirror record for {stream} exceeds the safe request size"
                            )
                        batches.append(batch)
                        batch = [record]
                        single = {"action": "ingest", "records": batch, "checkpoint": checkpoint}
                        if len(json.dumps(single, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_MIRROR_INGEST_BATCH_BYTES:
                            raise ValueError(
                                f"single durable mirror record for {stream} exceeds the safe request size"
                            )
                    else:
                        batch = candidate
                if batch or not batches:
                    batches.append(batch)
                seed_checkpoint = None
                if (mutable and len(batches) > 1
                        and (not isinstance(raw_checkpoint, dict)
                             or remote_meta.get("capture_epoch") != capture_epoch)):
                    first_batch_cursor = int(rows[len(batches[0]) - 1]["_cursor"])
                    seed_fingerprint = _advance_cursor_fingerprint_v2(
                        conn, stream, cursor, first_batch_cursor, cursor_fingerprint,
                        mutable=True,
                    )
                    seed_metadata = dict(checkpoint["metadata"])
                    seed_metadata["baseline_complete"] = False
                    seed_metadata["cursor_fingerprint"] = seed_fingerprint
                    seed_checkpoint = {
                        **checkpoint,
                        "last_cursor": str(first_batch_cursor),
                        "metadata": seed_metadata,
                    }
                plans.append({
                    "stream": stream, "mutable": mutable, "capture_epoch": capture_epoch or "",
                    "last_cursor": last_cursor, "fingerprint": new_fingerprint,
                    "checkpoint": checkpoint, "seed_checkpoint": seed_checkpoint,
                    "batches": batches,
                    "lagging": (len(rows) >= config.rows_per_stream or not baseline_complete
                                or last_cursor < local_high_water),
                })

            for plan in plans:
                _save_cursor_fingerprint(
                    conn, plan["stream"], plan["last_cursor"], mutable=plan["mutable"],
                    capture_epoch=plan["capture_epoch"], fingerprint=plan["fingerprint"],
                )
            conn.commit()

            mirrored = 0
            lagging = 0
            for plan in plans:
                batches = plan["batches"]
                for batch_index, records_batch in enumerate(batches):
                    request_body: dict[str, Any] = {"action": "ingest", "records": records_batch}
                    if batch_index == 0 and plan["seed_checkpoint"] is not None:
                        request_body["checkpoint"] = plan["seed_checkpoint"]
                    elif batch_index == len(batches) - 1:
                        request_body["checkpoint"] = plan["checkpoint"]
                    result = await _post(client, config, request_body)
                    mirrored += int(result.get("accepted") or 0)
                lagging += int(plan["lagging"])

            return {
                "status": "persisted",
                "mirrored": mirrored,
                "streams": len(plans),
                "lagging_streams": lagging,
                "present_streams": len(present),
            }
    finally:
        conn.close()


def _initialize_restore_schema(db_path: Path) -> None:
    """Create the current application schema without starting runtime services."""
    from .agent_store import AgentStore
    from .autonomous_research import ResearchWorkStore
    from .bill_tracker import BillTracker
    from .economic_ledger import EconomicLedger
    from .ecosystem_store import EcosystemStore
    from .ledger import ForecastLedger
    from .mission_store import MissionStore
    from .prediction_account_history import _ensure_schema as ensure_account_schema
    from .research_session import SessionStore
    from .research_trials import ResearchTrialStore

    stores = (
        AgentStore,
        MissionStore,
        EcosystemStore,
        ResearchTrialStore,
        ResearchWorkStore,
        ForecastLedger,
        EconomicLedger,
        BillTracker,
        SessionStore,
    )
    for store_type in stores:
        store = store_type(str(db_path))
        conn = getattr(store, "conn", None)
        if conn is not None:
            conn.close()

    with sqlite3.connect(db_path) as conn:
        ensure_account_schema(conn)
        conn.commit()


def _decode_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__noema_bytes_b64__"}:
            encoded = value["__noema_bytes_b64__"]
            if not isinstance(encoded, str):
                raise ValueError("invalid encoded byte value")
            try:
                return base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise ValueError("invalid encoded byte value") from exc
        if set(value) == {"__noema_integer__"}:
            encoded = value["__noema_integer__"]
            if not isinstance(encoded, str) or not re.fullmatch(r"-?(0|[1-9][0-9]*)", encoded):
                raise ValueError("invalid encoded integer value")
            return int(encoded)
        if set(value) == {"__noema_float_hex__"}:
            encoded = value["__noema_float_hex__"]
            if not isinstance(encoded, str):
                raise ValueError("invalid encoded float value")
            try:
                decoded = float.fromhex(encoded)
            except ValueError as exc:
                raise ValueError("invalid encoded float value") from exc
            if not math.isfinite(decoded):
                raise ValueError("invalid encoded float value")
            return decoded
        return {str(key): _decode_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_json_safe(item) for item in value]
    return value


def _validate_restore_record(
    name: str, record: Any, previous: int, through: int, *, require_source_projection: bool = False,
) -> int:
    if not isinstance(record, dict) or record.get("stream") != name:
        raise ValueError(f"restore stream {name} contains a malformed record")
    key = record.get("record_key")
    payload = record.get("payload")
    digest = record.get("version_sha256")
    if not isinstance(key, str) or not key.startswith(name + ":") or not isinstance(payload, dict):
        raise TypeError(f"restore stream {name} contains a malformed record")
    try:
        cursor = int(record.get("mirror_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"restore stream {name} has an invalid mirror cursor") from exc
    if cursor <= previous or cursor > through:
        raise ValueError(f"restore stream {name} cursors are unordered or out of range")
    expected_hash = (_legacy_canonical_hash(payload)
                     if record.get("source_schema_version") == "legacy-unversioned"
                     else _canonical_hash(payload))
    if not isinstance(digest, str) or expected_hash != digest:
        raise ValueError(f"restore record hash mismatch in {name}")
    source_projection = record.get("source_projection_sha256")
    legacy_projection = record.get("source_schema_version") == "legacy-unversioned"
    if require_source_projection and not legacy_projection:
        if not isinstance(source_projection, str) or not re.fullmatch(r"[a-f0-9]{64}", source_projection):
            raise ValueError(f"restore record has no source projection digest in {name}")
        if source_projection != _source_projection_hash(record):
            raise ValueError(f"restore source projection digest mismatch in {name}")
    projection_digest = record.get("projection_sha256")
    if not isinstance(projection_digest, str):
        raise TypeError(f"restore record has no projection integrity digest in {name}")
    if projection_digest != _restore_projection_hash(record):
        raise ValueError(f"restore projection metadata hash mismatch in {name}")
    if record.get("operation", "upsert") not in {"upsert", "tombstone"}:
        raise ValueError(f"restore stream {name} has an invalid operation")
    rowid = record.get("source_rowid")
    if rowid is not None and (not isinstance(rowid, int) or rowid < 0):
        raise ValueError(f"restore stream {name} has an invalid source rowid")
    if rowid is None and record.get("operation", "upsert") == "upsert":
        raise ValueError(f"restore stream {name} is missing its source rowid")
    source_event_id = record.get("source_event_id")
    if name in MUTABLE_STREAMS and (
        not isinstance(source_event_id, str) or not 1 <= len(source_event_id) <= 160
    ):
        raise ValueError(f"restore stream {name} has an invalid source event identity")
    return cursor


def _iter_bundle_records(
    path: str | Path, stream: str, *, connection: sqlite3.Connection | None = None,
) -> Iterator[dict[str, Any]]:
    owns_connection = connection is None
    conn = connection or sqlite3.connect(f"file:{Path(path).absolute()}?mode=ro", uri=True)
    try:
        cursor = conn.execute(
            "SELECT record_json FROM bundle_records WHERE stream=? ORDER BY mirror_id", (stream,),
        )
        for (record_json,) in cursor:
            record = json.loads(record_json)
            if not isinstance(record, dict):
                raise TypeError(f"streaming restore record in {stream} is malformed")
            yield record
    finally:
        if owns_connection:
            conn.close()


def _load_restore_bundle(bundle_path: str | Path | dict[str, Any]) -> dict[str, Any]:
    streaming_path: str | None = None
    if isinstance(bundle_path, dict):
        data = bundle_path
    else:
        path = Path(bundle_path)
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("restore bundle is empty")
        with path.open("rb") as handle:
            signature = handle.read(16)
        if signature == b"SQLite format 3\x00":
            conn = sqlite3.connect(f"file:{path.absolute()}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("streaming restore bundle failed its SQLite integrity check")
                row = conn.execute("SELECT manifest_json FROM bundle_manifest").fetchone()
                if row is None:
                    raise ValueError("streaming restore bundle has no manifest")
                data = json.loads(row[0])
                if not isinstance(data, dict) or data.get("format_version") != STREAMING_RESTORE_BUNDLE_VERSION:
                    raise ValueError("unsupported streaming restore bundle format or version")
                streams = data.get("streams")
                if not isinstance(streams, list):
                    raise TypeError("streaming restore bundle has no streams")
                for item in streams:
                    if not isinstance(item, dict):
                        raise TypeError("restore stream entry must be an object")
                    name = item.get("name")
                    actual = conn.execute(
                        "SELECT count(*) FROM bundle_records WHERE stream=?", (name,),
                    ).fetchone()[0]
                    if actual != item.get("record_count"):
                        raise ValueError(f"restore stream {name} record count does not match its manifest")
                    item["_streaming_records"] = True
                streaming_path = str(path.absolute())
            except (sqlite3.Error, json.JSONDecodeError) as exc:
                raise ValueError("streaming restore bundle is malformed") from exc
            finally:
                conn.close()
        else:
            if size > MAX_RESTORE_BUNDLE_BYTES:
                raise ValueError("restore bundle is empty or exceeds the size limit")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("restore bundle is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise TypeError("restore bundle root must be an object")
    if data.get("format") != RESTORE_BUNDLE_FORMAT or data.get("format_version") not in {
        RESTORE_BUNDLE_VERSION, STREAMING_RESTORE_BUNDLE_VERSION,
    }:
        raise ValueError("unsupported restore bundle format or version")
    if data.get("complete") is not True:
        raise ValueError("restore requires a complete, non-paginated export")
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ValueError("restore bundle must declare its streams")
    try:
        watermark = int(data["through_cursor"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("restore bundle has no valid watermark") from exc
    total = 0
    seen: set[str] = set()
    for item in streams:
        if not isinstance(item, dict):
            raise TypeError("restore stream entry must be an object")
        name = item.get("name")
        if not isinstance(name, str) or name not in DEFAULT_STREAMS or name in seen:
            raise ValueError("restore bundle contains an unknown or duplicate stream")
        seen.add(name)
        if item.get("from_cursor") != 0 or item.get("complete") is not True:
            raise ValueError(f"restore stream {name} is partial")
        if not isinstance(item.get("checkpoint"), dict):
            raise TypeError(f"restore stream {name} has no checkpoint metadata")
        try:
            through = int(item.get("through_cursor"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"restore stream {name} has an invalid checkpoint") from exc
        if through != watermark:
            raise ValueError(f"restore stream {name} does not share the bundle watermark")
        try:
            floor = int(item.get("restore_floor_id", 0))
            checkpoint = item["checkpoint"]
            checkpoint_metadata = checkpoint.get("metadata")
            if not isinstance(checkpoint_metadata, dict):
                raise TypeError("missing checkpoint metadata")
            checkpoint_cursor = int(checkpoint["last_cursor"])
            local_high_water = int(checkpoint_metadata["local_high_water"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"restore stream {name} has an invalid restore floor or checkpoint") from exc
        if floor < 0 or floor > watermark:
            raise ValueError(f"restore stream {name} has an out-of-range restore floor")
        if checkpoint_cursor < 0 or checkpoint_cursor != local_high_water:
            raise ValueError(f"restore stream {name} checkpoint cursor is inconsistent")
        expected_cursor_kind = "change_event_id" if name in MUTABLE_STREAMS else "rowid"
        if checkpoint_metadata.get("cursor_kind") != expected_cursor_kind:
            raise ValueError(f"restore stream {name} checkpoint cursor type is invalid")
        if checkpoint_metadata.get("baseline_complete") is not True:
            raise ValueError(f"restore stream {name} checkpoint is incomplete")
        if name in MUTABLE_STREAMS:
            try:
                checkpoint_floor = int(checkpoint_metadata["restore_floor_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"mutable stream {name} has no valid checkpoint restore floor") from exc
            if (checkpoint_floor != floor or checkpoint_floor < 0 or checkpoint_floor > watermark
                    or not checkpoint_metadata.get("capture_epoch")):
                raise ValueError(f"mutable stream {name} restore floor is inconsistent")
        elif floor != 0:
            raise ValueError(f"append-only stream {name} cannot declare a mutable restore floor")
        previous = 0
        if item.get("_streaming_records"):
            if streaming_path is None:
                raise ValueError("streaming restore records require a bundle path")
            records = _iter_bundle_records(streaming_path, name)
            expected_count = int(item.get("record_count", -1))
            if expected_count < 0:
                raise ValueError(f"restore stream {name} has an invalid record count")
        else:
            records = item.get("records")
            if not isinstance(records, list):
                raise TypeError(f"restore stream {name} records must be an array")
            expected_count = len(records)
            if int(item.get("record_count", -1)) != expected_count:
                raise ValueError(f"restore stream {name} record count does not match its manifest")
        for record in records:
            previous = _validate_restore_record(
                name, record, previous, through,
                require_source_projection=(data.get("format_version") == STREAMING_RESTORE_BUNDLE_VERSION),
            )
            total += 1
            if not item.get("_streaming_records") and total > MAX_RESTORE_RECORDS:
                raise ValueError("restore bundle exceeds the record limit")
    if seen != set(DEFAULT_STREAMS):
        missing = sorted(set(DEFAULT_STREAMS) - seen)
        raise ValueError("restore bundle omits critical streams: " + ", ".join(missing))
    if streaming_path is not None:
        data["_streaming_bundle_path"] = streaming_path
    return data


def restore_critical_state(
    bundle_path: str | Path,
    db_path: str | Path,
) -> dict[str, Any]:
    """Atomically restore a complete verified mirror export into a new SQLite file.

    Existing paths are never overwritten. The input format deliberately requires
    explicit complete checkpoints so a bounded page cannot masquerade as recovery.
    """
    target = Path(db_path).expanduser().absolute()
    if target.exists():
        raise FileExistsError("restore target already exists; recovery never overwrites local state")
    bundle = _load_restore_bundle(bundle_path)
    streaming_path = bundle.get("_streaming_bundle_path")
    bundle_connection: sqlite3.Connection | None = None
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.restore-", suffix=".db", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    counts: dict[str, int] = {}
    try:
        if streaming_path:
            bundle_connection = sqlite3.connect(f"file:{Path(streaming_path).absolute()}?mode=ro", uri=True)
            bundle_connection.execute("PRAGMA query_only=ON")
            bundle_connection.execute("BEGIN")
            if bundle_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("streaming restore bundle failed its SQLite integrity check")
            manifest_row = bundle_connection.execute(
                "SELECT manifest_json FROM bundle_manifest",
            ).fetchone()
            if manifest_row is None:
                raise ValueError("streaming restore bundle has no manifest")
            persisted_manifest = json.loads(manifest_row[0])
            expected_manifest = {key: value for key, value in bundle.items()
                                 if key != "_streaming_bundle_path"}
            for item in expected_manifest["streams"]:
                item.pop("_streaming_records", None)
            if persisted_manifest != expected_manifest:
                raise ValueError("streaming restore bundle changed after validation")
        _initialize_restore_schema(temp)
        conn = sqlite3.connect(temp, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            known_tables = _tables(conn)
            conn.execute("""CREATE TABLE noema_mirror_history (
                mirror_id INTEGER NOT NULL, stream TEXT NOT NULL, record_key TEXT NOT NULL,
                version_sha256 TEXT NOT NULL, operation TEXT NOT NULL, source_rowid INTEGER,
                source_event_id TEXT, occurred_at TEXT, source_commit TEXT, source_host TEXT,
                source_schema_version TEXT, source_projection_sha256 TEXT, payload_json TEXT NOT NULL,
                PRIMARY KEY(stream,mirror_id)
            )""")
            conn.execute("""CREATE TEMP TABLE restore_latest (
                stream TEXT NOT NULL, record_key TEXT NOT NULL, mirror_id INTEGER NOT NULL,
                record_json TEXT NOT NULL, PRIMARY KEY(stream,record_key)
            )""")
            for stream in bundle["streams"]:
                name = stream["name"]
                if name not in known_tables:
                    raise ValueError(f"current application schema does not define critical stream {name}")
                quoted = '"' + name.replace('"', '""') + '"'
                columns_info = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                columns = {str(row["name"]) for row in columns_info}
                restored = 0
                floor = int(stream["restore_floor_id"])
                records = (_iter_bundle_records(streaming_path, name, connection=bundle_connection)
                           if streaming_path else iter(stream["records"]))
                record_count = 0
                previous_cursor = 0
                for record in records:
                    previous_cursor = _validate_restore_record(
                        name, record, previous_cursor, int(stream["through_cursor"]),
                        require_source_projection=(
                            bundle.get("format_version") == STREAMING_RESTORE_BUNDLE_VERSION
                        ),
                    )
                    record_count += 1
                    _validate_restore_projection_identity(conn, name, record)
                    payload = record["payload"]
                    conn.execute(
                        "INSERT INTO noema_mirror_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (int(record["mirror_id"]), name, record["record_key"], record["version_sha256"],
                         record.get("operation", "upsert"), record.get("source_rowid"), record.get("source_event_id"),
                         record.get("occurred_at"), record.get("source_commit"), record.get("source_host"),
                         record.get("source_schema_version"), record.get("source_projection_sha256"),
                         json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)),
                    )
                    if int(record["mirror_id"]) > floor:
                        conn.execute("""INSERT INTO restore_latest VALUES(?,?,?,?)
                            ON CONFLICT(stream,record_key) DO UPDATE SET
                            mirror_id=excluded.mirror_id,record_json=excluded.record_json
                            WHERE excluded.mirror_id > restore_latest.mirror_id""",
                            (name, str(record["record_key"]), int(record["mirror_id"]),
                             json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))
                if streaming_path and record_count != int(stream["record_count"]):
                    raise ValueError(f"streaming restore stream {name} changed after validation")
                latest_cursor = conn.execute(
                    "SELECT record_json FROM restore_latest WHERE stream=? ORDER BY mirror_id", (name,),
                )
                for (record_json,) in latest_cursor:
                    record = json.loads(record_json)
                    if record.get("operation", "upsert") == "tombstone":
                        continue
                    payload = record["payload"]
                    if "rowid" in payload or any(not isinstance(key, str) for key in payload):
                        raise ValueError(f"invalid column set for {name}")
                    payload_columns = list(payload)
                    unknown = set(payload_columns) - columns
                    if unknown:
                        raise ValueError(f"restore schema mismatch in {name}: unknown columns")
                    for info in columns_info:
                        col = str(info["name"])
                        if (col not in payload and int(info["notnull"]) and info["dflt_value"] is None
                                and not int(info["pk"])):
                            raise ValueError(f"restore schema mismatch in {name}: required column unavailable")
                    cursor = record.get("source_rowid")
                    insert_columns = (["rowid"] if cursor is not None else []) + payload_columns
                    quoted_columns = ",".join('"' + key.replace('"', '""') + '"' for key in insert_columns)
                    placeholders = ",".join("?" for _ in insert_columns)
                    values = ([cursor] if cursor is not None else []) + [_decode_json_safe(payload[key]) for key in payload_columns]
                    conn.execute(f"INSERT INTO {quoted} ({quoted_columns}) VALUES ({placeholders})", values)
                    rowid = cursor if cursor is not None else conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    restored_row = conn.execute(f"SELECT * FROM {quoted} WHERE rowid=?", (rowid,)).fetchone()
                    restored_payload = ({key: restored_row[key] for key in payload_columns}
                                        if restored_row is not None else None)
                    restored_hash = (
                        _legacy_canonical_hash(restored_payload)
                        if restored_payload is not None and record.get("source_schema_version") == "legacy-unversioned"
                        else _canonical_hash(_json_safe(restored_payload)) if restored_payload is not None else None
                    )
                    if restored_row is None or restored_hash != record["version_sha256"]:
                        raise ValueError(f"restored row verification failed in {name}")
                    restored += 1
                counts[name] = restored
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise ValueError("restored SQLite integrity check failed")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Hard-link installation is atomic and fails if a concurrent process made
        # the destination; unlike replace(), it cannot overwrite that new file.
        os.chmod(temp, 0o600)
        os.link(temp, target)
        temp.unlink()
        return {
            "status": "restored_and_verified",
            "database": str(target),
            "streams": len(counts),
            "records": sum(counts.values()),
            "records_by_stream": counts,
            "integrity_check": "ok",
        }
    finally:
        if bundle_connection is not None:
            bundle_connection.close()
        for candidate in (temp, Path(str(temp) + "-wal"), Path(str(temp) + "-shm")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
