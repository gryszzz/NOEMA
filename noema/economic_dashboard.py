from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .economic_ledger import EconomicLedger
from .profit_waterfall import allocate_profit


def build_economic_overview(path: str = "data/noema.db") -> dict[str, Any]:
    if not Path(path).exists():
        return {
            "snapshot": None,
            "profit_plan": None,
            "latest_review": None,
        }

    ledger = EconomicLedger(path)
    snapshot = ledger.latest_snapshot()
    if snapshot is None:
        return {
            "snapshot": None,
            "profit_plan": None,
            "latest_review": _latest_review(path),
        }

    plan = allocate_profit(snapshot)
    return {
        "snapshot": {
            key: str(value)
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
        "latest_review": _latest_review(path),
    }


def _latest_review(path: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            """
            SELECT created_at, payload_json
            FROM economic_events
            WHERE event_type = 'economic_review'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    payload = json.loads(row[1])
    payload["created_at"] = row[0]
    return payload
