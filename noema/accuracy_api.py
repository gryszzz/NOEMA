"""Read-only accuracy leaderboard for the trading room.

The payload is safe to render: probabilities, scores, weights, and bench
status. It does not include credentials, prompts, or signer material.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .accuracy_config import AccuracyConfig
from .accuracy_influence import influence_for
from .accuracy_scoring import score_agents


def empty_accuracy_report(config: AccuracyConfig | None = None) -> dict[str, Any]:
    config = config or AccuracyConfig()
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "status": "no_forecasts",
        "paper_only": True,
        "agents": [],
        "thresholds": config.public_dict(),
    }


def build_accuracy_report(db_path: str, *, config: AccuracyConfig | None = None) -> dict[str, Any]:
    config = config or AccuracyConfig.from_env()
    if not Path(db_path).exists():
        report = empty_accuracy_report(config)
        report["status"] = "no_database"
        return report
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        agents = score_agents(conn, config=config)
    except sqlite3.Error:
        agents = []
    finally:
        conn.close()
    enriched = []
    for metrics in agents:
        influence = influence_for(metrics["agent_id"], metrics, config)
        row = {**metrics, **influence.as_dict()}
        row["agent"] = row["agent_id"]
        enriched.append(row)
    status = "ok" if enriched else "no_forecasts"
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "status": status,
        "paper_only": True,
        "agents": enriched,
        "thresholds": config.public_dict(),
    }
