from __future__ import annotations

import asyncio
import errno
import gzip
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .account_capital import value_native_wallets
from .agent_dashboard import build_agent_overview
from .bill_tracker import BillTracker
from .cognition_dashboard import build_cognition_overview, build_provider_health
from .config import kalshi_production_read_only_config
from .console_disk_inventory import build_console_disk_inventory
from .console_replication import (
    decode_worker_metadata,
    load_worker_metadata,
    persist_worker_metadata,
)
from .console_state import console_state_db_path
from .dashboard_data import build_overview
from .doctor import doctor_report
from .economic_dashboard import build_economic_overview
from .economic_measurement import build_economic_measurement
from .ecosystem_dashboard import build_ecosystem_overview
from .execution_gateway import ExecutionGateway
from .kalshi_telemetry import KalshiTelemetry
from .knowledge import build_knowledge_overview
from .ladder import build_ladder_report
from .live_balance import balance_history
from .market_qualification import build_market_data_qualification
from .operations_dashboard import build_operations
from .opportunity_radar import build_radar
from .paired_evaluation import compare_history_to_market
from .prediction_account_history import _ensure_schema as ensure_prediction_account_schema
from .prediction_venues import (
    build_prediction_venue_status,
    cached_prediction_venue_status,
)
from .research_state import research_trial_update_is_newer
from .sqlite_diagnostics import sqlite_error_fields
from .storage_recovery import recover_storage
from .stripe_economy import stripe_economy_overview
from .telemetry_report import build_telemetry_report
from .trench_dashboard import build_trench_overview
from .wallet_credentials import polymarket_us_credentials_present
from .wallet_diagnostics import live_wallet_networks, public_wallet_policy

app = FastAPI(title="NOEMA Ops Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
_wallet_status_cache: dict[str, Any] = {"fetched_at": 0.0, "networks": []}
_wallet_status_lock = asyncio.Lock()
_capital_sampler_task: asyncio.Task | None = None
_log = logging.getLogger(__name__)
# Uvicorn's production dictConfig attaches the stderr handler to this logger's
# parent (`uvicorn`). A custom logger propagates to root, which Uvicorn leaves
# handler-free, so even explicitly enabled INFO records can otherwise vanish.
_account_log = logging.getLogger("uvicorn.error")
_account_log.setLevel(logging.INFO)


_MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
_MAX_DATABASE_BYTES = 1024 * 1024 * 1024
_SNAPSHOT_IO_CHUNK_BYTES = 1024 * 1024
_snapshot_install_lock = asyncio.Lock()
_snapshot_request_sequence = 0
_snapshot_installed_sequence = 0
_storage_recovery_state: dict[str, object] = {"safe_to_write": True, "status": "unknown"}


def _check_snapshot_sqlite_integrity(snapshot_path: str) -> str | None:
    """Run the potentially full-database scan away from the ASGI event loop."""
    uri = Path(snapshot_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
    return None if row is None else row[0]


def _merge_account_history_into_console_state(source_path: str, state_path: str) -> None:
    """Import snapshot account evidence into the non-replaceable console database."""
    source_path = str(Path(source_path).resolve())
    state = Path(state_path)
    if not Path(source_path).is_file() or Path(source_path) == state.resolve():
        return
    source_uri = Path(source_path).as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=5) as source:
        source_tables = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        retained_tables = {
            "prediction_account_records", "prediction_account_sync_state",
            "prediction_account_baselines", "live_balance_observations",
            "canonical_pair_observations", "autonomous_research_runs", "research_trials",
            "economic_events",
        }
        if not source_tables.intersection(retained_tables):
            return
        state.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state, timeout=30) as target:
            target.execute("PRAGMA busy_timeout=30000")
            _copy_account_history_tables(source, target, source_tables)
            _copy_console_research_history(source, target, source_tables)


def _enable_console_state_wal(state_path: str) -> None:
    """Allow the persistent sidecar's account readers and snapshot writer to coexist."""
    state = Path(state_path)
    state.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")


def _copy_account_history_tables(
    source: sqlite3.Connection, target: sqlite3.Connection, source_tables: set[str],
) -> None:
    """Merge immutable/idempotent account rows; never replace the writer database."""
    retained_tables = {
        "prediction_account_records", "prediction_account_sync_state",
        "prediction_account_baselines", "live_balance_observations",
    }
    if not source_tables.intersection(retained_tables):
        return
    ensure_prediction_account_schema(target)
    for table, columns in (
        ("prediction_account_records", "venue,record_type,external_id,market_id,related_order_id,occurred_at,first_observed_at,last_observed_at,state,payload_json"),
        ("prediction_account_sync_state", "venue,stream,last_success_at,last_full_audit_at,high_water_at,high_water_id,last_error_type"),
    ):
        if table not in source_tables:
            continue
        if table == "prediction_account_records":
            for row in source.execute(f"SELECT {columns} FROM {table}"):
                key = row[:3]
                current = target.execute(
                    "SELECT first_observed_at,last_observed_at FROM prediction_account_records "
                    "WHERE venue=? AND record_type=? AND external_id=?", key,
                ).fetchone()
                first_seen = _earliest_timestamp(row[6], current[0] if current else None)
                if current and not _timestamp_is_newer(row[7], current[1]):
                    if first_seen != current[0]:
                        target.execute(
                            "UPDATE prediction_account_records SET first_observed_at=? "
                            "WHERE venue=? AND record_type=? AND external_id=?",
                            (first_seen, *key),
                        )
                    continue
                target.execute(
                    "INSERT INTO prediction_account_records "
                    f"({columns}) VALUES ({','.join('?' for _ in columns.split(','))}) "
                    "ON CONFLICT(venue,record_type,external_id) DO UPDATE SET "
                    "market_id=excluded.market_id, related_order_id=excluded.related_order_id, "
                    "occurred_at=excluded.occurred_at, first_observed_at=excluded.first_observed_at, "
                    "last_observed_at=excluded.last_observed_at, state=excluded.state, "
                    "payload_json=excluded.payload_json",
                    (*row[:6], first_seen, *row[7:]),
                )
        elif table == "prediction_account_sync_state":
            for row in source.execute(f"SELECT {columns} FROM {table}"):
                key = row[:2]
                current = target.execute(
                    "SELECT last_success_at,last_full_audit_at,high_water_at,high_water_id,last_error_type "
                    "FROM prediction_account_sync_state WHERE venue=? AND stream=?", key,
                ).fetchone()
                if current is None:
                    target.execute(
                        f"INSERT INTO {table} ({columns}) VALUES "
                        f"({','.join('?' for _ in columns.split(','))})", row,
                    )
                    continue
                success = _later_timestamp(row[2], current[0])
                audit = _later_timestamp(row[3], current[1])
                incoming_high_water_is_newer = _timestamp_is_newer(row[4], current[2])
                high_water_at = row[4] if incoming_high_water_is_newer else current[2]
                high_water_id = row[5] if incoming_high_water_is_newer else current[3]
                incoming_state_at = _latest_sync_timestamp(row[2:4], row[4])
                existing_state_at = _latest_sync_timestamp(current[:2], current[2])
                error = row[6] if _timestamp_is_newer(incoming_state_at, existing_state_at) else current[4]
                target.execute(
                    "UPDATE prediction_account_sync_state SET last_success_at=?, "
                    "last_full_audit_at=?, high_water_at=?, high_water_id=?, last_error_type=? "
                    "WHERE venue=? AND stream=?",
                    (success, audit, high_water_at, high_water_id, error, *key),
                )
    if "prediction_account_baselines" in source_tables:
        target.execute("""CREATE TABLE IF NOT EXISTS prediction_account_baselines (
                venue TEXT PRIMARY KEY, observed_at TEXT NOT NULL, cash_usd TEXT NOT NULL,
                portfolio_value_usd TEXT NOT NULL, source TEXT NOT NULL)""")
        target.executemany("""INSERT OR IGNORE INTO prediction_account_baselines
                (venue,observed_at,cash_usd,portfolio_value_usd,source) VALUES (?,?,?,?,?)""",
            source.execute("SELECT venue,observed_at,cash_usd,portfolio_value_usd,source "
                           "FROM prediction_account_baselines"))
    if "live_balance_observations" in source_tables:
        target.execute("""CREATE TABLE IF NOT EXISTS live_balance_observations (
                fingerprint TEXT PRIMARY KEY, observed_at TEXT NOT NULL, amount_usd TEXT NOT NULL,
                scope TEXT NOT NULL, sources_json TEXT NOT NULL)""")
        target.executemany("""INSERT OR IGNORE INTO live_balance_observations
                (fingerprint,observed_at,amount_usd,scope,sources_json) VALUES (?,?,?,?,?)""",
            source.execute("SELECT fingerprint,observed_at,amount_usd,scope,sources_json "
                           "FROM live_balance_observations"))
    target.commit()


def _copy_console_research_history(
    source: sqlite3.Connection, target: sqlite3.Connection, source_tables: set[str],
) -> None:
    """Merge durable pair evidence and its run/settlement history idempotently."""
    for table in (
        "canonical_pair_observations", "autonomous_research_runs", "research_trials",
    ):
        if table not in source_tables:
            continue
        definition = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if not definition or not definition[0]:
            continue
        target.execute(definition[0].replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
        source_columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
        target_columns = {row[1] for row in target.execute(f"PRAGMA table_info({table})")}
        missing_columns = [row for row in source.execute(f"PRAGMA table_info({table})")
                           if row[1] not in target_columns]
        for _, name, data_type, _, default, _ in missing_columns:
            declaration = f"ALTER TABLE {table} ADD COLUMN {name} {data_type or 'TEXT'}"
            if default is not None:
                declaration += f" DEFAULT {default}"
            target.execute(declaration)
        target_columns = {row[1] for row in target.execute(f"PRAGMA table_info({table})")}
        if table == "research_trials" and "status_updated_at" not in target_columns:
            target.execute("ALTER TABLE research_trials ADD COLUMN status_updated_at TEXT")
        columns = [name for name in source_columns if name != "id"]
        if not columns:
            continue
        rows = source.execute(f"SELECT {','.join(columns)} FROM {table}")
        if table == "research_trials" and {"trial_id", "status"} <= set(source_columns):
            status_time_column = next((name for name in ("status_updated_at", "updated_at")
                                       if name in columns), None)
            for row in rows:
                values = dict(zip(columns, row, strict=True))
                trial_id = values["trial_id"]
                status = values["status"]
                existing = target.execute(
                    "SELECT status,status_updated_at FROM research_trials WHERE trial_id=?", (trial_id,),
                ).fetchone()
                if existing is None:
                    target.execute(
                        f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)})", row,
                    )
                elif research_trial_update_is_newer(
                    status, values.get(status_time_column) if status_time_column else None,
                    existing[0], existing[1],
                ):
                    target.execute(
                        "UPDATE research_trials SET status=?,status_updated_at=? WHERE trial_id=?",
                        (status, values.get(status_time_column) if status_time_column else None, trial_id),
                    )
        elif table == "autonomous_research_runs" and {
            "trial_id", "evidence_hash", "worker_version", "status", "completed_at",
        } <= set(source_columns):
            update_columns = [name for name in columns if name not in {
                "trial_id", "evidence_hash", "worker_version",
            }]
            for row in rows:
                existing = target.execute(
                    "SELECT completed_at FROM autonomous_research_runs "
                    "WHERE trial_id=? AND evidence_hash=? AND worker_version=?",
                    (row[columns.index("trial_id")], row[columns.index("evidence_hash")],
                     row[columns.index("worker_version")]),
                ).fetchone()
                incoming_completed = row[columns.index("completed_at")]
                if existing is not None and incoming_completed is not None and (
                    existing[0] is None or str(incoming_completed) >= str(existing[0])
                ):
                    target.execute(
                        f"UPDATE autonomous_research_runs SET "
                        f"{','.join(f'{name}=?' for name in update_columns)} "
                        "WHERE trial_id=? AND evidence_hash=? AND worker_version=?",
                        tuple(row[columns.index(name)] for name in update_columns) + (
                            row[columns.index("trial_id")], row[columns.index("evidence_hash")],
                            row[columns.index("worker_version")],
                        ),
                    )
                    continue
                target.execute(
                    f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})", row,
                )
        else:
            insert_sql = (
                f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})"
            )
            for row in rows:
                target.execute(insert_sql, row)

    if "economic_events" not in source_tables:
        target.commit()
        return
    definition = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='economic_events'",
    ).fetchone()
    if definition and definition[0]:
        target.execute(definition[0].replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
    source_columns = [row[1] for row in source.execute("PRAGMA table_info(economic_events)")]
    target_columns = {row[1] for row in target.execute("PRAGMA table_info(economic_events)")}
    for _, name, data_type, _, default, _ in source.execute("PRAGMA table_info(economic_events)"):
        if name in target_columns:
            continue
        declaration = f"ALTER TABLE economic_events ADD COLUMN {name} {data_type or 'TEXT'}"
        if default is not None:
            declaration += f" DEFAULT {default}"
        target.execute(declaration)
        target_columns.add(name)
    for name, declaration in {
        "provider": "TEXT", "external_reference_id": "TEXT", "occurred_at": "TEXT",
        "currency": "TEXT", "amount": "TEXT", "mission_id": "TEXT", "strategy_id": "TEXT",
        "activity_id": "TEXT", "lane": "TEXT", "evidence_json": "TEXT",
        "reconciliation_state": "TEXT", "value_state": "TEXT", "capital_class": "TEXT",
        "confidence_state": "TEXT", "completeness_state": "TEXT", "related_event_id": "INTEGER",
        "owned_account_id": "TEXT", "counterparty_account_id": "TEXT",
    }.items():
        if name not in target_columns:
            target.execute(f"ALTER TABLE economic_events ADD COLUMN {name} {declaration}")
            target_columns.add(name)
    columns = [name for name in source_columns if name in target_columns and name != "id"]
    required = {"created_at", "event_type", "payload_json"}
    if not required <= set(columns):
        target.commit()
        return
    target.execute("""CREATE TABLE IF NOT EXISTS console_imported_research_events (
        event_hash TEXT PRIMARY KEY)""")
    selected = ",".join(columns)
    existing_events = target.execute(
        "SELECT " + selected + " FROM economic_events WHERE "
        "event_type='canonical_market_observation' OR event_type LIKE 'paper_cross_venue_%' "
        "OR event_type IN ('paper_settlement','paper_result')")
    for row in existing_events:
        digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
        target.execute(
            "INSERT OR IGNORE INTO console_imported_research_events VALUES (?)", (digest,),
        )
    rows = source.execute(
        "SELECT " + selected + " FROM economic_events WHERE "
        "event_type='canonical_market_observation' OR event_type LIKE 'paper_cross_venue_%' "
        "OR event_type IN ('paper_settlement','paper_result')")
    placeholders = ",".join("?" for _ in columns)
    for row in rows:
        digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
        exists = target.execute(
            "SELECT 1 FROM console_imported_research_events WHERE event_hash=?", (digest,),
        ).fetchone()
        if exists:
            continue
        target.execute(
            f"INSERT OR IGNORE INTO economic_events ({selected}) VALUES ({placeholders})", row,
        )
        target.execute(
            "INSERT OR IGNORE INTO console_imported_research_events VALUES (?)", (digest,),
        )
    target.commit()


def _parsed_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _timestamp_is_newer(candidate: Any, current: Any) -> bool:
    candidate_at = _parsed_timestamp(candidate)
    current_at = _parsed_timestamp(current)
    return candidate_at is not None and (current_at is None or candidate_at > current_at)


def _later_timestamp(left: Any, right: Any) -> Any:
    return left if _timestamp_is_newer(left, right) else right


def _earliest_timestamp(left: Any, right: Any) -> Any:
    left_at, right_at = _parsed_timestamp(left), _parsed_timestamp(right)
    if left_at is None:
        return right
    if right_at is None or left_at < right_at:
        return left
    return right


def _latest_sync_timestamp(success_fields: Any, high_water: Any) -> datetime | None:
    candidates = [*success_fields, high_water]
    parsed = [_parsed_timestamp(item) for item in candidates]
    valid = [item for item in parsed if item is not None]
    return max(valid) if valid else None


def _constant_time_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@app.middleware("http")
async def protect_console(request: Request, call_next):
    """Protect every console surface and accept worker replication on its own token."""
    path = request.url.path
    if path == "/healthz":
        return await call_next(request)
    if path == "/internal/snapshot":
        configured = os.getenv("NOEMA_CONSOLE_SNAPSHOT_TOKEN", "")
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {configured}" if configured else ""
        if not configured:
            return JSONResponse({"detail": "snapshot receiver is not configured"}, status_code=503)
        if not _constant_time_equal(supplied, expected):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    username = os.getenv("NOEMA_CONSOLE_USERNAME", "")
    password = os.getenv("NOEMA_CONSOLE_PASSWORD", "")
    if path == "/api/disk-inventory" and (not username or not password):
        return JSONResponse({"detail": "console authentication is not configured"}, status_code=503)
    if os.getenv("NOEMA_CONSOLE_AUTH_REQUIRED") == "1" and (not username or not password):
        return JSONResponse({"detail": "console authentication is not configured"}, status_code=503)
    if username or password:
        supplied = request.headers.get("authorization", "")
        valid = False
        if supplied.startswith("Basic ") and username and password:
            import base64

            try:
                decoded = base64.b64decode(supplied[6:], validate=True).decode("utf-8")
                supplied_user, separator, supplied_password = decoded.partition(":")
                valid = bool(separator) and _constant_time_equal(supplied_user, username)
                valid = valid and _constant_time_equal(supplied_password, password)
            except (ValueError, UnicodeDecodeError):
                valid = False
        if not valid:
            return JSONResponse(
                {"detail": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="NOEMA Operations", charset="UTF-8"'},
            )
    return await call_next(request)



def _capital_sample_interval() -> int:
    try:
        return max(15, min(300, int(os.getenv("NOEMA_CAPITAL_SAMPLE_INTERVAL_SECONDS", "15"))))
    except ValueError:
        return 15


def _capital_history_sampler_enabled() -> bool:
    return os.getenv("NOEMA_CAPITAL_HISTORY_SAMPLER_ENABLED", "1").strip().lower() not in {
        "0", "false", "no",
    }


def _capital_sample_sleep_seconds(interval: int, cycle_started: float,
                                 now: float | None = None) -> float:
    """Keep the sampler start-to-start cadence bounded by its target interval."""
    elapsed = max(0.0, (time.monotonic() if now is None else now) - cycle_started)
    return max(0.0, interval - elapsed)


async def _sample_capital_history() -> None:
    """Persist observed account marks on a bounded cadence, independent of page clients."""
    while True:
        cycle_started = time.monotonic()
        stage = "worker_database_check"
        if not Path(_db_path()).is_file():
            _account_log.warning("Capital history sampler status=waiting_for_worker_snapshot")
            await asyncio.sleep(_capital_sample_sleep_seconds(
                _capital_sample_interval(), cycle_started,
            ))
            continue
        _account_log.info("Capital history sampler cycle started")
        try:
            # Bypass request caches: this sampler owns the collection cadence,
            # so each runtime cycle must perform a new authenticated read.
            stage = "authenticated_source_reads"
            wallets, venues = await asyncio.gather(wallet_status(force=True), prediction_venues(force=True))
            venue_rows = venues.get("venues", [])
            polymarket_rows = [
                row for row in venue_rows
                if isinstance(row, dict)
                and str(row.get("venue", "")).lower().startswith("polymarket")
            ]
            if not polymarket_rows:
                _account_log.info(
                    "Account observation venue=polymarket_us status=projection_missing"
                )
            for venue in venue_rows:
                if not isinstance(venue, dict):
                    continue
                account = venue.get("account") or {}
                if str(venue.get("venue", "")).lower().startswith("polymarket"):
                    coverage = account.get("history_coverage") or {}
                    activity_coverage = coverage.get("activities") or {}
                    persistence = account.get("history_persistence") or {}
                    stream = account.get("private_stream") or account.get("update_transport") or {}
                    account_read = account.get("account_read") or {}
                    _account_log.info(
                        "Account observation venue=polymarket_us status=%s balance_available=%s "
                        "positions=%s fills=%s activities=%s activity_complete=%s "
                        "missing_persisted=%s persistence=%s inserted=%s updated=%s skipped=%s "
                        "account_stage=%s account_failure=%s account_http_status=%s "
                        "private_stream=%s stream_events=%s stream_reconnects=%s "
                        "stream_error_type=%s stream_last_message_at=%s stream_persisted_at=%s",
                        account.get("status", "unknown"), account.get("balance_available"),
                        account.get("positions"), account.get("fills"),
                        account.get("activity_records"), activity_coverage.get("complete"),
                        activity_coverage.get("missing_persisted_records"),
                        persistence.get("status", "unavailable"), persistence.get("inserted"),
                        persistence.get("updated"), persistence.get("skipped"),
                        account_read.get("stage", "none"),
                        account_read.get("classification", "none"),
                        account_read.get("http_status", "none"),
                        stream.get("state", "unknown"), stream.get("events_received"),
                        stream.get("reconnects"), stream.get("last_error_type", "none"),
                        stream.get("last_message_at"),
                        stream.get("last_persisted_at"),
                    )
            stage = "persist_balance_history"
            stripe = await asyncio.to_thread(stripe_economy_overview, _db_path())
            await asyncio.to_thread(
                balance_history, _console_state_db_path(), venues,
                {"networks": wallets.get("networks", []), "observed_at": wallets.get("observed_at")},
                window="24H", stripe=stripe,
            )
            _account_log.info(
                "Capital history sampler cycle completed venues=%d wallet_networks=%d",
                len(venue_rows), len(wallets.get("networks", [])),
            )
        except Exception as exc:  # noqa: BLE001 - persist safe stage/type and keep sampler retrying.
            _account_log.warning(
                "Capital history sampler failed stage=%s error_type=%s",
                stage, type(exc).__name__,
            )
        await asyncio.sleep(_capital_sample_sleep_seconds(
            _capital_sample_interval(), cycle_started,
        ))


@app.on_event("startup")
async def start_capital_sampler() -> None:
    global _capital_sampler_task, _storage_recovery_state
    report = await asyncio.to_thread(
        recover_storage,
        _console_state_db_path(),
        role="console",
        additional_sqlite_paths=(_db_path(),),
    )
    _storage_recovery_state = {"status": "ready" if report.safe_to_write else "degraded_read_only", **report.safe_fields()}
    _account_log.info("Console storage recovery %s", json.dumps(_storage_recovery_state, sort_keys=True))
    if not report.safe_to_write:
        _account_log.error("Console persistent storage below safe write floor; starting read-only")
        return
    try:
        _enable_console_state_wal(_console_state_db_path())
        _merge_account_history_into_console_state(_db_path(), _console_state_db_path())
    except (OSError, sqlite3.Error) as exc:
        _storage_recovery_state = {
            "status": "degraded_read_only",
            "safe_to_write": False,
            "error_type": type(exc).__name__,
            **sqlite_error_fields(exc),
        }
        _account_log.error("Console startup storage write failed error_type=%s", type(exc).__name__)
        return
    sampler_enabled = _capital_history_sampler_enabled()
    key_id_present, secret_present = polymarket_us_credentials_present()
    _account_log.info(
        "Capital observation startup sampler_enabled=%s sample_interval_seconds=%s "
        "worker_database_present=%s polymarket_key_id_present=%s polymarket_secret_present=%s",
        sampler_enabled, _capital_sample_interval(), Path(_db_path()).is_file(),
        key_id_present, secret_present,
    )
    if sampler_enabled:
        _capital_sampler_task = asyncio.create_task(_sample_capital_history(), name="noema-capital-history")
    else:
        _account_log.warning("Capital history sampler is disabled by configuration")


@app.on_event("shutdown")
async def stop_capital_sampler() -> None:
    global _capital_sampler_task
    for task in (_capital_sampler_task,):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _capital_sampler_task = None


def _db_path() -> str:
    return os.getenv("NOEMA_DB_PATH", "data/noema.db")


def _console_state_db_path() -> str:
    return console_state_db_path(_db_path())


def _worker_provider_health() -> dict[str, Any]:
    metadata = load_worker_metadata(_db_path())
    if metadata is None:
        return {
            "source": "worker snapshot metadata unavailable",
            "configured_provider": "unknown",
            "configured_provider_ready": None,
            "connectivity": "not probed by console",
        }
    cognition = metadata["cognition"]
    provider = str(cognition["provider"])
    configured = cognition["configured"]
    status = ("configuration_ready_connectivity_unverified" if configured is True
              else "not_ready_or_unconfigured" if configured is False else "unknown")
    provider_rows = {
        name: {"status": status if provider == name else "not_current_worker_route",
               "model": cognition["deployment"] if provider == name else None,
               "credential_present": None}
        for name in ("openai", "cloudflare_workers_ai", "foundry", "docker_model_runner")
    }
    provider_rows.update({
        "groq": {"status": "not_current_worker_route", "credential_present": None},
        "chronos": {"status": "not_probed_by_console"},
        "finbert": {"status": "not_probed_by_console"},
        "specialists": {"status": "not_probed_by_console"},
        "local_model_runner": {"status": "not_probed_by_console"},
    })
    return {
        **provider_rows,
        "source": "worker environment metadata paired with latest database snapshot",
        "configured_provider": provider,
        "configured_provider_ready": configured,
        "connectivity": "not probed by console",
        "worker_commit": metadata["worker_commit"],
    }


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Render health check; it reveals no operational data or credentials."""
    path = Path(_db_path())
    storage_writable = bool(_storage_recovery_state.get("safe_to_write", True))
    status = (
        "degraded_storage_read_only"
        if not storage_writable
        else "ready" if path.is_file() else "awaiting_worker_snapshot"
    )
    return {
        "status": status,
        "database_present": path.is_file(),
        "storage_writable": storage_writable,
    }


@app.get("/api/storage-health")
def storage_health() -> dict[str, object]:
    """Secret-free storage recovery telemetry for the authenticated console."""
    return dict(_storage_recovery_state)


@app.get("/api/runtime")
def runtime_info() -> dict[str, Any]:
    path = Path(_db_path())
    try:
        stat = path.stat()
    except OSError:
        return {
            "database_present": False,
            "source": os.getenv("NOEMA_RUNTIME_SOURCE", "local"),
            "deployment_commit": os.getenv("RENDER_GIT_COMMIT"),
            "worker": load_worker_metadata(_db_path()),
        }
    age_seconds = max(0, int(time.time() - stat.st_mtime))
    stale_after_seconds = max(900, 3 * int(os.getenv("NOEMA_AGENT_CYCLE_SECONDS", "300")))
    return {
        "database_present": True,
        "source": os.getenv("NOEMA_RUNTIME_SOURCE", "local"),
        "snapshot_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "age_seconds": age_seconds,
        "snapshot_stale": age_seconds > stale_after_seconds,
        "deployment_commit": os.getenv("RENDER_GIT_COMMIT"),
        "worker": load_worker_metadata(_db_path()),
        "database_bytes": stat.st_size,
    }


@app.get("/api/disk-inventory")
def console_disk_inventory() -> dict[str, Any]:
    """Owner-authenticated file metadata for the console volume; file contents are never read."""
    try:
        return build_console_disk_inventory()
    except OSError as exc:
        _log.warning("Console disk inventory unavailable error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="console disk inventory unavailable") from None


@app.post("/internal/snapshot")
async def receive_worker_snapshot(request: Request) -> dict[str, Any]:
    """Atomically replace the worker snapshot without replacing console-owned state."""
    global _snapshot_request_sequence
    _snapshot_request_sequence += 1
    request_sequence = _snapshot_request_sequence
    return await _receive_worker_snapshot(request, request_sequence)


async def _receive_worker_snapshot(
    request: Request, request_sequence: int,
) -> dict[str, Any]:
    """Validate a snapshot and install it without allowing stale concurrent requests to win."""
    global _snapshot_installed_sequence
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_SNAPSHOT_BYTES:
                raise HTTPException(
                    status_code=413, detail="snapshot is too large",
                    headers={
                        "X-NOEMA-Snapshot-Rejection": "compressed_size_limit",
                        "X-NOEMA-Snapshot-Rejected-Bytes": str(int(content_length)),
                    },
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
    destination = Path(_db_path()).resolve()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path, image_size = await _decompress_snapshot_to_file(request, destination.parent)
    except HTTPException:
        raise
    except Exception as exc:
        _log_snapshot_storage_failure("write_snapshot", exc)
        raise HTTPException(status_code=500, detail="snapshot could not be persisted") from exc
    encoded_metadata = request.headers.get("x-noema-worker-metadata")
    try:
        worker_metadata = None if encoded_metadata is None else decode_worker_metadata(encoded_metadata)
    except (TypeError, ValueError) as exc:
        Path(temporary_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="worker metadata is invalid") from exc

    failure_stage = "write_snapshot"
    try:
        try:
            check = await asyncio.to_thread(_check_snapshot_sqlite_integrity, temporary_path)
        except sqlite3.Error as exc:
            raise HTTPException(status_code=400, detail="snapshot is not a valid SQLite database") from exc
        else:
            if check != "ok":
                raise HTTPException(status_code=400, detail="snapshot failed SQLite integrity check")
        async with _snapshot_install_lock:
            if request_sequence < _snapshot_installed_sequence:
                return {"status": "superseded", "database_bytes": image_size,
                        "received_at": datetime.now(UTC).isoformat()}
            # Account rows included in worker snapshots are imported into the
            # console-owned sidecar. Live console writers target that same file,
            # which this atomic worker replacement never touches.
            failure_stage = "merge_console_state"
            # This merge can wait for a concurrent account sampler/private-stream
            # SQLite writer (busy_timeout is intentionally bounded but may be long).
            # Keep that wait off the ASGI event loop so health, SSE, and other console
            # requests remain responsive while SQLite serializes the writers.
            await asyncio.to_thread(
                _merge_account_history_into_console_state,
                temporary_path,
                _console_state_db_path(),
            )
            # The replica is an immutable worker-provided snapshot. Console-owned
            # state is persisted in the sidecar, never in this replaceable file.
            failure_stage = "replace_worker_replica"
            os.chmod(temporary_path, 0o444)
            os.replace(temporary_path, destination)
            if worker_metadata is not None:
                failure_stage = "persist_worker_metadata"
                persist_worker_metadata(_db_path(), worker_metadata)
            _snapshot_installed_sequence = request_sequence
    except HTTPException:
        raise
    except Exception as exc:
        # Snapshot failures can involve sensitive account payloads. Log only a
        # fixed stage name and exception class; never the exception message,
        # request headers, or snapshot contents.
        _log_snapshot_storage_failure(failure_stage, exc)
        raise HTTPException(status_code=500, detail="snapshot could not be persisted") from exc
    finally:
        Path(temporary_path).unlink(missing_ok=True)
    return {"status": "persisted", "database_bytes": image_size,
            "received_at": datetime.now(UTC).isoformat()}


def _log_snapshot_storage_failure(stage: str, exc: Exception) -> None:
    """Log snapshot failure class and errno only; never exception text or content."""
    error_number = exc.errno if isinstance(exc, OSError) else None
    error_name = errno.errorcode.get(error_number) if error_number is not None else None
    disk_total = disk_free = replica_bytes = state_bytes = temp_snapshot_bytes = None
    if error_number == errno.ENOSPC:
        try:
            replica = Path(_db_path()).resolve()
            usage = shutil.disk_usage(replica.parent)
            disk_total, disk_free = usage.total, usage.free
            state = Path(_console_state_db_path()).resolve()
            replica_bytes = _file_size_or_none(replica)
            state_bytes = _file_size_or_none(state)
            temp_snapshot_bytes = sum(
                size for item in replica.parent.iterdir()
                if item.name.startswith("tmp") and item.suffix in {".sqlite3", ".gz"}
                and (size := _file_size_or_none(item)) is not None
            )
        except OSError:
            # Storage diagnostics are best-effort and must not mask the original failure.
            pass
    _log.error(
        "Worker snapshot persistence failed stage=%s exception_type=%s errno=%s errno_name=%s "
        "sqlite_error_code=%s sqlite_error_name=%s disk_total_bytes=%s disk_free_bytes=%s "
        "replica_bytes=%s console_state_bytes=%s temp_snapshot_bytes=%s",
        stage, type(exc).__name__, error_number, error_name or "unknown",
        sqlite_error_fields(exc).get("sqlite_error_code"),
        sqlite_error_fields(exc).get("sqlite_error_name"),
        disk_total, disk_free, replica_bytes, state_bytes, temp_snapshot_bytes,
    )


def _file_size_or_none(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


async def _decompress_snapshot_to_file(request: Request, directory: Path) -> tuple[str, int]:
    """Stream a bounded gzip snapshot to disk instead of buffering it in RAM."""
    compressed_path: str | None = None
    database_path: str | None = None
    compressed_size = 0
    database_size = 0
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as compressed:
            compressed_path = compressed.name
            async for chunk in request.stream():
                compressed_size += len(chunk)
                if compressed_size > _MAX_SNAPSHOT_BYTES:
                    raise HTTPException(
                        status_code=413, detail="snapshot is too large",
                        headers={
                            "X-NOEMA-Snapshot-Rejection": "compressed_size_limit",
                            "X-NOEMA-Snapshot-Rejected-Bytes": str(compressed_size),
                        },
                    )
                await asyncio.to_thread(compressed.write, chunk)
            await asyncio.to_thread(compressed.flush)
            await asyncio.to_thread(os.fsync, compressed.fileno())

        database_path, database_size = await asyncio.to_thread(
            _inflate_snapshot_file, compressed_path, directory,
        )
    except HTTPException:
        if database_path is not None:
            Path(database_path).unlink(missing_ok=True)
        raise
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        if database_path is not None:
            Path(database_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="invalid snapshot encoding") from exc
    finally:
        if compressed_path is not None:
            Path(compressed_path).unlink(missing_ok=True)
    if database_path is None:
        raise HTTPException(status_code=400, detail="invalid snapshot encoding")
    return database_path, database_size


def _inflate_snapshot_file(compressed_path: str, directory: Path) -> tuple[str, int]:
    """Expand a gzip database to a temporary file with a strict size ceiling."""
    database_path: str | None = None
    database_size = 0
    try:
        with gzip.open(compressed_path, "rb") as inflater, tempfile.NamedTemporaryFile(
            dir=directory, delete=False,
        ) as database:
            database_path = database.name
            while chunk := inflater.read(_SNAPSHOT_IO_CHUNK_BYTES):
                database_size += len(chunk)
                if database_size > _MAX_DATABASE_BYTES:
                    raise HTTPException(
                        status_code=413, detail="database snapshot is too large",
                        headers={
                            "X-NOEMA-Snapshot-Rejection": "database_size_limit",
                            "X-NOEMA-Snapshot-Rejected-Bytes": str(database_size),
                        },
                    )
                database.write(chunk)
            database.flush()
            os.fsync(database.fileno())
    except HTTPException:
        if database_path is not None:
            Path(database_path).unlink(missing_ok=True)
        raise
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        if database_path is not None:
            Path(database_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="invalid snapshot encoding") from exc
    except OSError:
        if database_path is not None:
            Path(database_path).unlink(missing_ok=True)
        raise
    if database_path is None:
        raise HTTPException(status_code=400, detail="invalid snapshot encoding")
    return database_path, database_size


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    path = Path(__file__).with_name("static") / "index.html"
    return path.read_text()


async def _runtime_change_stream(request: Request):
    """Push invalidations for worker snapshots and console-owned account commits."""
    paths = (Path(_db_path()).resolve(), Path(_console_state_db_path()).resolve())
    if not paths[0].is_file():
        yield "event: unavailable\ndata: {}\n\n"
        return

    def versions() -> tuple[tuple[int, int, int, int] | None, ...]:
        values = []
        for watched_path in paths:
            # SQLite WAL commits may change only the -wal file until a
            # checkpoint. Observe it alongside the main database so an SSE
            # client is invalidated immediately after a sidecar commit.
            for current_path in (watched_path, Path(f"{watched_path}-wal")):
                try:
                    stat = current_path.stat()
                    values.append((stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))
                except FileNotFoundError:
                    values.append(None)
        return tuple(values)

    try:
        version = versions()
        yield "event: ready\ndata: {}\n\n"
        last_keepalive = time.monotonic()
        while not await request.is_disconnected():
            await asyncio.sleep(1)
            current = versions()
            if current != version:
                version = current
                yield "event: change\ndata: {}\n\n"
                last_keepalive = time.monotonic()
            elif time.monotonic() - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()
    except OSError:
        yield "event: unavailable\ndata: {}\n\n"


@app.get("/api/runtime-stream")
async def runtime_stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _runtime_change_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/detailed", response_class=HTMLResponse)
async def detailed() -> str:
    return (Path(__file__).with_name("static") / "detailed.html").read_text()


@app.get("/api/operations")
def operations() -> dict[str, Any]:
    return build_operations(_db_path(), additional_paths=(_console_state_db_path(),))


@app.get("/api/knowledge")
def knowledge() -> dict[str, Any]:
    return build_knowledge_overview(_db_path())


@app.get("/api/agent")
async def agent() -> dict[str, Any]:
    return build_agent_overview(_db_path())


@app.get("/api/doctor")
async def doctor() -> dict[str, Any]:
    return doctor_report(_db_path())


@app.get("/api/ladder")
async def ladder() -> dict[str, Any]:
    return build_ladder_report(_db_path())


@app.get("/api/compare")
async def compare() -> dict[str, Any]:
    return compare_history_to_market(_db_path()).as_dict()


@app.get("/api/market-data-qualification")
def market_data_qualification() -> dict[str, Any]:
    """Current research/data readiness, kept separate from execution authority."""
    return build_market_data_qualification(_db_path())


@app.get("/api/cognition")
async def cognition() -> dict[str, Any]:
    replica = os.getenv("NOEMA_RUNTIME_SOURCE") == "worker-sqlite-replica"
    overview = build_cognition_overview(_db_path(), probe_runtime=not replica)
    if replica:
        metadata = load_worker_metadata(_db_path())
        if metadata is None:
            overview.update({"enabled": None, "configured": None,
                             "credential_present": None,
                             "runtime_providers": _worker_provider_health()})
        else:
            cognition_metadata = metadata["cognition"]
            overview.update({
                "enabled": cognition_metadata["enabled"],
                "configured": cognition_metadata["configured"],
                "provider": cognition_metadata["provider"],
                "deployment": cognition_metadata["deployment"],
                "credential_present": None,
                "runtime_providers": _worker_provider_health(),
                "worker_commit": metadata["worker_commit"],
            })
    return overview


@app.get("/api/provider-health")
def provider_health() -> dict[str, Any]:
    if os.getenv("NOEMA_RUNTIME_SOURCE") == "worker-sqlite-replica":
        return _worker_provider_health()
    return build_provider_health()


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    return build_overview(_db_path())


@app.get("/api/economy")
async def economy() -> dict[str, Any]:
    return build_economic_overview(_db_path(), additional_paths=(_console_state_db_path(),))


@app.get("/api/stripe-economy")
def stripe_economy() -> dict[str, Any]:
    """Secret-free projection of the latest Stripe observations persisted by the agent."""
    return stripe_economy_overview(_db_path())


@app.get("/api/ecosystem")
async def ecosystem() -> dict[str, Any]:
    return build_ecosystem_overview(_db_path())


@app.get("/api/economic-measurement")
async def economic_measurement() -> dict[str, Any]:
    return build_economic_measurement(_db_path(), additional_paths=(_console_state_db_path(),))


@app.get("/api/bill")
async def bill() -> dict[str, Any]:
    return BillTracker(_db_path()).overview()


@app.get("/api/radar")
async def radar() -> list[dict[str, Any]]:
    return [row.__dict__ for row in build_radar(_db_path())]


@app.get("/api/trench")
async def trench() -> dict[str, Any]:
    return build_trench_overview(_db_path())


@app.get("/api/wallet-policy")
async def wallet_policy() -> dict[str, Any]:
    return public_wallet_policy()


@app.get("/api/execution-gateway")
async def execution_gateway_status() -> dict[str, Any]:
    return ExecutionGateway(_db_path(), initialize=False).overview()


@app.get("/api/wallet-status")
async def wallet_status(*, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if force or now - float(_wallet_status_cache["fetched_at"]) > 15:
        async with _wallet_status_lock:
            now = time.monotonic()
            if force or now - float(_wallet_status_cache["fetched_at"]) > 15:
                networks = await live_wallet_networks()
                _wallet_status_cache["networks"] = await value_native_wallets(networks)
                _wallet_status_cache["fetched_at"] = time.monotonic()
                _wallet_status_cache["observed_at"] = datetime.now(UTC).isoformat()
    policy = public_wallet_policy()
    policy_by_chain = {row["chain"]: row for row in policy.get("wallet_networks", [])}
    networks = []
    for cached in _wallet_status_cache["networks"]:
        row = dict(cached)
        row["observed_at"] = _wallet_status_cache.get("observed_at")
        current = policy_by_chain.get(row.get("chain"), {})
        # Balance/connection evidence is briefly cached; authority/configuration
        # flags are recomputed on every request so stale settings cannot appear live.
        for field in (
            "signer_configured", "credentials_isolated", "signer_process_enabled", "halted",
            "mission_authority_present", "live_execution_enabled", "coordinator_wired",
        ):
            if field in current:
                row[field] = current[field]
        row["signing_enabled"] = False
        networks.append(row)
    return {
        "as_of_monotonic": _wallet_status_cache["fetched_at"],
        "observed_at": _wallet_status_cache.get("observed_at"),
        "refresh_interval_seconds": 15,
        "control_plane": {
            "live_execution_enabled": False,
            "mission_authority_present": False,
            "coordinator_wired": False,
            "halted": policy["master_halt"],
            "status": "SIGNER MAY BE CONFIGURED · LIVE EXECUTION DISABLED · COORDINATOR NOT WIRED",
        },
        "networks": networks,
    }


@app.get("/api/capital-history")
async def capital_history(window: Literal["1H", "24H", "7D", "30D", "ALL"] = "24H") -> dict:
    wallets = {"networks": _wallet_status_cache["networks"],
               "observed_at": _wallet_status_cache.get("observed_at")}
    return await asyncio.to_thread(balance_history, _console_state_db_path(), cached_prediction_venue_status(),
                                   wallets, window=window, stripe=stripe_economy_overview(_db_path()))


@app.get("/api/live-account")
async def live_account() -> dict[str, Any]:
    try:
        telemetry = KalshiTelemetry(kalshi_production_read_only_config())
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Kalshi credentials are not configured for read-only telemetry.",
        ) from exc

    try:
        orders, fills, positions = await __import__("asyncio").gather(
            telemetry.orders(),
            telemetry.fills(),
            telemetry.positions(),
        )
        try:
            settlements = await telemetry.settlements()
        except Exception as exc:  # noqa: BLE001 - retain partial account coverage on provider errors
            settlements = []
            telemetry.coverage["settlements"] = {
                "complete": False, "pages": 0, "records": 0,
                "error_type": type(exc).__name__,
            }
        report = build_telemetry_report(
            orders=orders,
            fills=fills,
            positions=positions,
            settlements=settlements,
        )
        report["history_coverage"] = dict(telemetry.coverage)
        return report
    finally:
        await telemetry.close()


@app.get("/api/prediction-venues")
async def prediction_venues(*, force: bool = False) -> dict[str, Any]:
    """Current official read-only status for supported prediction venues."""
    return await build_prediction_venue_status(force=force)
