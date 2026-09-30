from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .economic_accounting import validate_snapshot
from .economic_ledger import EconomicLedger
from .economic_models import EconomicSnapshot
from .profit_waterfall import allocate_profit


def build_economic_overview(
    path: str = "data/noema.db", *, additional_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not Path(path).exists():
        return {
            "snapshot": None,
            "profit_plan": None,
            "latest_review": None,
            "canonical_ledger": EconomicLedger.read_projection(
                path, additional_paths=additional_paths,
            ),
        }

    snapshot = _latest_snapshot(path)
    if snapshot is None:
        return {
            "snapshot": None,
            "profit_plan": None,
            "latest_review": _latest_review((path, *additional_paths)),
            "canonical_ledger": EconomicLedger.read_projection(
                path, additional_paths=additional_paths,
            ),
        }

    plan = allocate_profit(snapshot)
    return {
        "snapshot": {
            key: str(value) if value is not None else None
            for key, value in asdict(snapshot).items()
        },
        "profit_plan": {
            "profit_above_high_water_usd": str(
                plan.profit_above_high_water_usd
            ),
            "allocations": {
                bucket.value: str(amount)
                for bucket, amount in plan.allocations.items()
            },
        },
        "latest_review": _latest_review((path, *additional_paths)),
        "canonical_ledger": EconomicLedger.read_projection(
            path, additional_paths=additional_paths,
        ),
    }


def _latest_snapshot(path: str) -> EconomicSnapshot | None:
    database = Path(path)
    if not database.is_file():
        return None
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='economic_snapshots'"
            ).fetchone()
            if table is None:
                return None
            row = conn.execute(
                "SELECT snapshot_json FROM economic_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        raw = json.loads(row[0])
        snapshot = EconomicSnapshot(**{
            key: None if value is None else Decimal(str(value)) for key, value in raw.items()
        })
        validate_snapshot(snapshot)
        return snapshot
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return None


def _latest_review(paths: tuple[str, ...]) -> dict[str, Any] | None:
    candidates: list[tuple[str, str]] = []
    for path in paths:
        database = Path(path)
        if not database.is_file():
            continue
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                row = conn.execute(
                    "SELECT created_at,payload_json FROM economic_events "
                    "WHERE event_type='economic_review' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            if row:
                candidates.append((str(row[0]), str(row[1])))
        except sqlite3.Error:
            continue
    if not candidates:
        return None
    created_at, raw_payload = max(candidates, key=lambda item: item[0])
    try:
        payload = json.loads(raw_payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["created_at"] = created_at
    return payload
