from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import time
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
from .polymarket_account_stream import run_polymarket_account_stream
from .prediction_account_history import _ensure_schema as ensure_prediction_account_schema
from .prediction_venues import (
    apply_polymarket_stream_projection,
    build_prediction_venue_status,
    cached_prediction_venue_status,
)
from .research_state import research_trial_update_is_newer
from .stripe_economy import stripe_economy_overview
from .telemetry_report import build_telemetry_report
from .trench_dashboard import build_trench_overview
from .wallet_diagnostics import live_wallet_networks, public_wallet_policy

app = FastAPI(title="NOEMA Ops Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
_wallet_status_cache: dict[str, Any] = {"fetched_at": 0.0, "networks": []}
_wallet_status_lock = asyncio.Lock()
_capital_sampler_task: asyncio.Task | None = None
_polymarket_stream_task: asyncio.Task | None = None
_log = logging.getLogger(__name__)


_MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
_MAX_DATABASE_BYTES = 512 * 1024 * 1024


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
        with sqlite3.connect(state, timeout=5) as target:
            _copy_account_history_tables(source, target, source_tables)
            _copy_console_research_history(source, target, source_tables)


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
        rows = source.execute(f"SELECT {columns} FROM {table}").fetchall()
        if table == "prediction_account_records":
            for row in rows:
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
            for row in rows:
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
                           "FROM prediction_account_baselines").fetchall())
    if "live_balance_observations" in source_tables:
        target.execute("""CREATE TABLE IF NOT EXISTS live_balance_observations (
                fingerprint TEXT PRIMARY KEY, observed_at TEXT NOT NULL, amount_usd TEXT NOT NULL,
                scope TEXT NOT NULL, sources_json TEXT NOT NULL)""")
        target.executemany("""INSERT OR IGNORE INTO live_balance_observations
                (fingerprint,observed_at,amount_usd,scope,sources_json) VALUES (?,?,?,?,?)""",
            source.execute("SELECT fingerprint,observed_at,amount_usd,scope,sources_json "
                           "FROM live_balance_observations").fetchall())
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
        rows = source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
        if table == "research_trials" and {"trial_id", "status"} <= set(source_columns):
            status_time_column = next((name for name in ("status_updated_at", "updated_at")
                                       if name in columns), None)
            pending = []
            for row in rows:
                values = dict(zip(columns, row, strict=True))
                trial_id = values["trial_id"]
                status = values["status"]
                existing = target.execute(
                    "SELECT status,status_updated_at FROM research_trials WHERE trial_id=?", (trial_id,),
                ).fetchone()
                if existing is None:
                    pending.append(row)
                elif research_trial_update_is_newer(
                    status, values.get(status_time_column) if status_time_column else None,
                    existing[0], existing[1],
                ):
                    target.execute(
                        "UPDATE research_trials SET status=?,status_updated_at=? WHERE trial_id=?",
                        (status, values.get(status_time_column) if status_time_column else None, trial_id),
                    )
            rows = pending
        if table == "autonomous_research_runs" and {
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
        target.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})", rows,
        )

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
        "OR event_type IN ('paper_settlement','paper_result')"
    ).fetchall()
    for row in existing_events:
        digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
        target.execute(
            "INSERT OR IGNORE INTO console_imported_research_events VALUES (?)", (digest,),
        )
    rows = source.execute(
        "SELECT " + selected + " FROM economic_events WHERE "
        "event_type='canonical_market_observation' OR event_type LIKE 'paper_cross_venue_%' "
        "OR event_type IN ('paper_settlement','paper_result')"
    ).fetchall()
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


def _capital_sample_sleep_seconds(interval: int, cycle_started: float,
                                 now: float | None = None) -> float:
    """Keep the sampler start-to-start cadence bounded by its target interval."""
    elapsed = max(0.0, (time.monotonic() if now is None else now) - cycle_started)
    return max(0.0, interval - elapsed)


async def _sample_capital_history() -> None:
    """Persist observed account marks on a bounded cadence, independent of page clients."""
    while True:
        cycle_started = time.monotonic()
        if not Path(_db_path()).is_file():
            await asyncio.sleep(_capital_sample_sleep_seconds(
                _capital_sample_interval(), cycle_started,
            ))
            continue
        try:
            # Bypass request caches: this sampler owns the collection cadence,
            # so each runtime cycle must perform a new authenticated read.
            wallets, venues = await asyncio.gather(wallet_status(force=True), prediction_venues(force=True))
            stripe = await asyncio.to_thread(stripe_economy_overview, _db_path())
            await asyncio.to_thread(
                balance_history, _console_state_db_path(), venues,
                {"networks": wallets.get("networks", []), "observed_at": wallets.get("observed_at")},
                window="24H", stripe=stripe,
            )
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error, TimeoutError):
            _log.warning("Capital history sampler could not complete an observation")
        await asyncio.sleep(_capital_sample_sleep_seconds(
            _capital_sample_interval(), cycle_started,
        ))


@app.on_event("startup")
async def start_capital_sampler() -> None:
    global _capital_sampler_task, _polymarket_stream_task
    # Import console-owned rows written by the previous colocated-database
    # version before enabling the sidecar writers. SQLite uniqueness makes
    # concurrent web-process startup migrations idempotent.
    _merge_account_history_into_console_state(_db_path(), _console_state_db_path())
    if os.getenv("NOEMA_CAPITAL_HISTORY_SAMPLER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}:
        _capital_sampler_task = asyncio.create_task(_sample_capital_history(), name="noema-capital-history")
    _polymarket_stream_task = asyncio.create_task(
        run_polymarket_account_stream(_console_state_db_path, apply_polymarket_stream_projection),
        name="noema-polymarket-account-stream",
    )


@app.on_event("shutdown")
async def stop_capital_sampler() -> None:
    global _capital_sampler_task, _polymarket_stream_task
    for task in (_capital_sampler_task, _polymarket_stream_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _capital_sampler_task = None
    _polymarket_stream_task = None


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
    return {
        "status": "ready" if path.is_file() else "awaiting_worker_snapshot",
        "database_present": path.is_file(),
    }


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


@app.post("/internal/snapshot")
async def receive_worker_snapshot(request: Request) -> dict[str, Any]:
    """Atomically replace the worker snapshot without replacing console-owned state."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_SNAPSHOT_BYTES:
                raise HTTPException(status_code=413, detail="snapshot is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
    compressed = await request.body()
    if len(compressed) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="snapshot is too large")
    try:
        inflater = gzip.GzipFile(fileobj=__import__("io").BytesIO(compressed))
        image = inflater.read(_MAX_DATABASE_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise HTTPException(status_code=400, detail="invalid snapshot encoding") from exc
    if len(image) > _MAX_DATABASE_BYTES:
        raise HTTPException(status_code=413, detail="database snapshot is too large")
    encoded_metadata = request.headers.get("x-noema-worker-metadata")
    try:
        worker_metadata = None if encoded_metadata is None else decode_worker_metadata(encoded_metadata)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="worker metadata is invalid") from exc

    destination = Path(_db_path()).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = temporary.name
            temporary.write(image)
            temporary.flush()
            os.fsync(temporary.fileno())
        uri = Path(temporary_path).resolve().as_uri() + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as conn:
                check = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=400, detail="snapshot is not a valid SQLite database") from exc
        else:
            if check is None or check[0] != "ok":
                raise HTTPException(status_code=400, detail="snapshot failed SQLite integrity check")
        # Account rows included in worker snapshots are imported into the
        # console-owned sidecar. Live console writers target that same file,
        # which this atomic worker replacement never touches.
        _merge_account_history_into_console_state(temporary_path, _console_state_db_path())
        # The replica is an immutable worker-provided snapshot. Console-owned
        # state is persisted in the sidecar, never in this replaceable file.
        os.chmod(temporary_path, 0o444)
        os.replace(temporary_path, destination)
        temporary_path = None
        if worker_metadata is not None:
            persist_worker_metadata(_db_path(), worker_metadata)
    except HTTPException:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=500, detail="snapshot could not be persisted") from exc
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
    return {"status": "persisted", "database_bytes": len(image),
            "received_at": datetime.now(UTC).isoformat()}


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
            try:
                stat = watched_path.stat()
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
