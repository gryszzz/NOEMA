from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .trench_collector import TrenchCollectorStore
from .trench_survival_model import audit_database


def build_trench_overview(
    path: str = "data/noema.db",
    *,
    limit: int = 20,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not Path(path).exists():
        return {
            "database_present": False,
            "counts": {"launches": 0, "observations": 0, "attempts": 0},
            "recent_launches": [],
            "recent_candidates": [],
            "survival_model": None,
        }

    store = TrenchCollectorStore(path)
    launches = store.conn.execute(
        """
        SELECT mint, first_pool_at, discovered_at, last_seen_at, active
        FROM trench_launches
        ORDER BY first_pool_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    try:
        candidates = store.conn.execute(
            """
            SELECT token_mint, captured_at, disposition, survival_risk,
                   opportunity_score, assessment_json
            FROM trench_candidates
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        candidates = []

    return {
        "database_present": True,
        "counts": store.counts(),
        "recent_launches": [
            {
                "mint": str(mint),
                "first_pool_at": str(first_pool_at),
                "discovered_at": str(discovered_at),
                "last_seen_at": str(last_seen_at),
                "active": bool(active),
            }
            for mint, first_pool_at, discovered_at, last_seen_at, active in launches
        ],
        "survival_model": audit_database(path).as_dict(),
        "recent_candidates": [
            {
                "mint": str(mint),
                "captured_at": str(captured_at),
                "disposition": str(disposition),
                "survival_risk": float(survival_risk),
                "opportunity_score": float(opportunity_score),
                "assessment": json.loads(str(assessment_json)),
            }
            for (
                mint,
                captured_at,
                disposition,
                survival_risk,
                opportunity_score,
                assessment_json,
            ) in candidates
        ],
    }
