"""Idempotent persistence for normalized, authenticated venue account records."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prediction_account_records (
            venue TEXT NOT NULL,
            record_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            market_id TEXT,
            related_order_id TEXT,
            occurred_at TEXT,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            state TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (venue, record_type, external_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS prediction_account_records_market "
                 "ON prediction_account_records(venue, market_id, occurred_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prediction_account_sync_state (
            venue TEXT NOT NULL,
            stream TEXT NOT NULL,
            last_success_at TEXT,
            last_full_audit_at TEXT,
            high_water_at TEXT,
            high_water_id TEXT,
            last_error_type TEXT,
            PRIMARY KEY (venue, stream)
        )
    """)


def load_prediction_account_records(
    path: str | Path, venue: str, *, record_types: tuple[str, ...] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read normalized persisted source rows for local reconciliation only."""
    database = Path(path)
    if not database.is_file():
        return {}
    conn = sqlite3.connect(database, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        query = "SELECT record_type,payload_json FROM prediction_account_records WHERE venue=?"
        args: list[Any] = [venue]
        if record_types:
            query += " AND record_type IN (" + ",".join("?" for _ in record_types) + ")"
            args.extend(record_types)
        query += " ORDER BY occurred_at, external_id"
        rows = conn.execute(query, args).fetchall()
        output: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                output.setdefault(str(row["record_type"]), []).append(payload)
        return output
    finally:
        conn.close()


def prediction_account_sync_state(path: str | Path, venue: str) -> dict[str, dict[str, Any]]:
    database = Path(path)
    if not database.is_file():
        return {}
    conn = sqlite3.connect(database, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        return {str(row["stream"]): dict(row) for row in conn.execute(
            "SELECT * FROM prediction_account_sync_state WHERE venue=?", (venue,),
        ).fetchall()}
    finally:
        conn.close()


def get_or_create_prediction_account_baseline(
    path: str | Path, venue: str, *, observed_at: str, cash_usd: str | None,
    portfolio_value_usd: str | None, positions_complete: bool, open_positions: int | None,
) -> dict[str, Any] | None:
    """Anchor future realized-P&L reconciliation only at an official flat snapshot."""
    if (not venue or not positions_complete or open_positions != 0
            or cash_usd is None or portfolio_value_usd is None):
        return None
    try:
        timestamp = datetime.fromisoformat(observed_at)
        if timestamp.tzinfo is None:
            return None
        cash = Decimal(cash_usd)
        portfolio = Decimal(portfolio_value_usd)
        if not cash.is_finite() or not portfolio.is_finite():
            return None
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prediction_account_baselines (
                venue TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                cash_usd TEXT NOT NULL,
                portfolio_value_usd TEXT NOT NULL,
                source TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO prediction_account_baselines
                (venue,observed_at,cash_usd,portfolio_value_usd,source)
            VALUES (?,?,?,?,?)
        """, (venue, timestamp.astimezone(UTC).isoformat(), format(cash, "f"),
              format(portfolio, "f"), "authenticated_balance.updated_ts + complete_flat_positions"))
        conn.commit()
        row = conn.execute("SELECT * FROM prediction_account_baselines WHERE venue=?", (venue,)).fetchone()
        return None if row is None else dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_id(kind: str, row: dict[str, Any]) -> str | None:
    if kind == "fill":
        value = row.get("fill_id")
    elif kind == "order":
        value = row.get("order_id")
    elif kind == "settlement":
        ticker, settled = row.get("ticker"), row.get("settled_time")
        value = row.get("settlement_id") or (f"{ticker}:{settled}:{row.get('exchange_index', 0)}" if ticker and settled else None)
    elif kind == "position_resolution":
        value = row.get("trade_id")
    elif kind == "balance_activity":
        value = row.get("transaction_id")
    elif kind in {"deposit", "withdrawal", "transfer"}:
        value = row.get("id") or row.get("transfer_id") or row.get("transaction_id")
    elif kind == "position":
        value = f"{row.get('ticker')}:{row.get('outcome') or ''}" if row.get("ticker") else None
    elif kind == "balance":
        value = row.get("observed_at")
    else:
        value = None
    return str(value) if value not in (None, "", "None:") else None


def persist_prediction_account_records(
    path: str | Path, venue_records: list[dict[str, Any]], *, observed_at: str | None = None,
) -> dict[str, Any]:
    """Persist source rows once, updating mutable order/position state in place.

    `venue_records` must contain already normalized account records. Credentials
    and raw HTTP response bodies are deliberately not accepted by this layer.
    """
    now = observed_at or datetime.now(UTC).isoformat()
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=5)
    conn.row_factory = sqlite3.Row
    inserted = updated = skipped = 0
    try:
        _ensure_schema(conn)
        for entry in venue_records:
            venue = str(entry.get("venue") or "")
            if not venue:
                skipped += 1
                continue
            coverage = entry.get("coverage") if isinstance(entry.get("coverage"), dict) else {}
            current_positions: set[str] = set()
            collections = {
                "balance": "balance", "position": "positions", "fill": "fills",
                "order": "orders", "settlement": "settlements",
                "position_resolution": "position_resolutions",
                "balance_activity": "balance_activities",
                "deposit": "deposits", "withdrawal": "withdrawals", "transfer": "transfers",
            }
            for kind, collection in collections.items():
                rows = entry.get(collection, [])
                if kind == "balance":
                    rows = entry.get("balance")
                if kind == "balance":
                    rows = [rows] if isinstance(rows, dict) else []
                if not isinstance(rows, list):
                    skipped += 1
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        skipped += 1
                        continue
                    external_id = _record_id(kind, row)
                    if external_id is None:
                        skipped += 1
                        continue
                    market_id = row.get("ticker") or row.get("market_slug")
                    related_order = row.get("order_id")
                    occurred_at = (row.get("created_time") or row.get("created_at") or row.get("settled_time")
                                   or row.get("last_updated_ts") or row.get("observed_at"))
                    if occurred_at is None and row.get("created_ts") is not None:
                        try:
                            occurred_at = datetime.fromtimestamp(float(row["created_ts"]), UTC).isoformat()
                        except (TypeError, ValueError, OverflowError):
                            occurred_at = None
                    state = row.get("status") or row.get("state")
                    if kind == "position":
                        try:
                            open_position = Decimal(str(row.get("position_fp", "0"))) != 0
                        except (InvalidOperation, TypeError, ValueError):
                            open_position = False
                        state = "open" if open_position else "flat"
                        if open_position:
                            current_positions.add(external_id)
                    elif kind == "settlement" and state is None:
                        state = "settled"
                    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
                    previous = conn.execute(
                        "SELECT payload_json,state FROM prediction_account_records "
                        "WHERE venue=? AND record_type=? AND external_id=?",
                        (venue, kind, external_id),
                    ).fetchone()
                    if (kind == "position" and previous is not None
                            and previous["state"] == "settled" and state == "flat"):
                        state = "settled"
                    if previous is not None and previous["payload_json"] == payload and previous["state"] == state:
                        skipped += 1
                        continue
                    conn.execute("""
                        INSERT INTO prediction_account_records
                            (venue,record_type,external_id,market_id,related_order_id,occurred_at,
                             first_observed_at,last_observed_at,state,payload_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(venue,record_type,external_id) DO UPDATE SET
                            market_id=excluded.market_id,
                            related_order_id=COALESCE(excluded.related_order_id, prediction_account_records.related_order_id),
                            occurred_at=COALESCE(excluded.occurred_at, prediction_account_records.occurred_at),
                            last_observed_at=excluded.last_observed_at,
                            state=COALESCE(excluded.state, prediction_account_records.state),
                            payload_json=excluded.payload_json
                    """, (venue, kind, external_id, market_id, related_order, occurred_at,
                          now, now, state, payload))
                    if previous is not None:
                        updated += 1
                    else:
                        inserted += 1
            if coverage.get("positions_complete") is True:
                active = conn.execute(
                    "SELECT external_id FROM prediction_account_records "
                    "WHERE venue=? AND record_type='position' AND state='open'", (venue,),
                ).fetchall()
                for row in active:
                    if row["external_id"] not in current_positions:
                        conn.execute("UPDATE prediction_account_records SET state='not_currently_open', "
                                     "last_observed_at=? WHERE venue=? AND record_type='position' AND external_id=?",
                                     (now, venue, row["external_id"]))
                        updated += 1
            if coverage.get("open_orders_complete") is True:
                current_order_ids = {
                    str(row.get("order_id")) for row in entry.get("orders", [])
                    if isinstance(row, dict) and row.get("order_id")
                }
                active_orders = conn.execute(
                    "SELECT external_id FROM prediction_account_records "
                    "WHERE venue=? AND record_type='order' AND lower(COALESCE(state,'')) "
                    "IN ('open','resting','pending','partially_filled')", (venue,),
                ).fetchall()
                for row in active_orders:
                    if row["external_id"] not in current_order_ids:
                        conn.execute("UPDATE prediction_account_records SET state='not_open_in_current_snapshot', "
                                     "last_observed_at=? WHERE venue=? AND record_type='order' AND external_id=?",
                                     (now, venue, row["external_id"]))
                        updated += 1
            if coverage.get("settlements_complete") is True:
                settled = conn.execute(
                    "SELECT market_id FROM prediction_account_records "
                    "WHERE venue=? AND record_type='settlement' AND market_id IS NOT NULL", (venue,),
                ).fetchall()
                for row in settled:
                    conn.execute("UPDATE prediction_account_records SET state='settled', last_observed_at=? "
                                 "WHERE venue=? AND record_type='position' AND market_id=? "
                                 "AND COALESCE(state,'') <> 'settled'",
                                 (now, venue, row["market_id"]))
            sync = entry.get("sync_state") if isinstance(entry.get("sync_state"), dict) else {}
            for stream, state in sync.items():
                if not isinstance(state, dict):
                    continue
                conn.execute("""
                    INSERT INTO prediction_account_sync_state
                        (venue,stream,last_success_at,last_full_audit_at,high_water_at,high_water_id,last_error_type)
                    VALUES (?,?,?,?,?,?,NULL)
                    ON CONFLICT(venue,stream) DO UPDATE SET
                        last_success_at=COALESCE(excluded.last_success_at,prediction_account_sync_state.last_success_at),
                        last_full_audit_at=COALESCE(excluded.last_full_audit_at,prediction_account_sync_state.last_full_audit_at),
                        high_water_at=COALESCE(excluded.high_water_at,prediction_account_sync_state.high_water_at),
                        high_water_id=COALESCE(excluded.high_water_id,prediction_account_sync_state.high_water_id),
                        last_error_type=NULL
                """, (venue, stream, now if state.get("success") is True and state.get("complete") is True else None,
                      now if state.get("full_audit_complete") is True else None,
                      state.get("high_water_at") if state.get("complete") is True else None,
                      state.get("high_water_id") if state.get("complete") is True else None))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"status": "persisted", "inserted": inserted, "updated": updated, "skipped": skipped}
