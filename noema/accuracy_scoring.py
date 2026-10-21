"""Per-agent Brier, log loss, reliability, hit rate, edge, and rolling windows."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .accuracy_config import AccuracyConfig
from .evaluation import calibration_buckets, expected_calibration_error


def _round(value: float | None, places: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), places)


def _metrics(rows: list[sqlite3.Row], *, bucket_width: float) -> dict[str, Any]:
    if not rows:
        return {
            "sample_size": 0,
            "brier": None,
            "log_loss": None,
            "calibration_error": None,
            "calibration": [],
            "hit_rate": None,
            "mean_edge": None,
            "mean_ev_after_cost": None,
            "hits": 0,
            "hit_sample_size": 0,
        }
    pairs = [(float(row["predicted_probability"]), int(row["outcome"])) for row in rows]
    brier = sum(float(row["brier"]) for row in rows) / len(rows)
    log_loss = sum(float(row["log_loss"]) for row in rows) / len(rows)
    buckets = calibration_buckets(pairs, bucket_width=bucket_width)
    hits = [row for row in rows if row["hit"] is not None]
    hit_rate = None if not hits else sum(int(row["hit"]) for row in hits) / len(hits)
    edges = [float(row["edge"]) for row in rows if row["edge"] is not None]
    evs = [float(row["ev_after_cost"]) for row in rows if row["ev_after_cost"] is not None]
    return {
        "sample_size": len(rows),
        "brier": _round(brier),
        "log_loss": _round(log_loss),
        "calibration_error": _round(expected_calibration_error(pairs, bucket_width=bucket_width)),
        "calibration": [
            {
                "lower": _round(bucket.lower, 2),
                "upper": _round(bucket.upper, 2),
                "count": bucket.count,
                "mean_probability": _round(bucket.mean_probability),
                "observed_rate": _round(bucket.observed_rate),
                "absolute_error": _round(bucket.absolute_error),
            }
            for bucket in buckets
        ],
        "hit_rate": _round(hit_rate),
        "hits": sum(int(row["hit"]) for row in hits),
        "hit_sample_size": len(hits),
        "mean_edge": _round(sum(edges) / len(edges)) if edges else None,
        "mean_ev_after_cost": _round(sum(evs) / len(evs)) if evs else None,
    }


def score_agents(
    conn: sqlite3.Connection,
    *,
    config: AccuracyConfig | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Score each agent that has recorded a forecast. Unresolved agents stay unscored."""
    config = config or AccuracyConfig()
    now = now or datetime.now(UTC)
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        forecasts = conn.execute(
            "SELECT agent_id, COUNT(*) AS n FROM accuracy_forecasts GROUP BY agent_id"
        ).fetchall()
        resolved = conn.execute(
            """SELECT f.agent_id, f.recorded_at, f.predicted_probability, f.decision,
                      r.outcome, r.brier, r.log_loss, r.hit, r.edge, r.ev_after_cost
               FROM accuracy_forecasts f
               JOIN accuracy_resolutions r ON r.forecast_id = f.id"""
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.row_factory = previous_factory
    by_agent: dict[str, list[sqlite3.Row]] = {}
    for row in resolved:
        by_agent.setdefault(row["agent_id"], []).append(row)
    reports: list[dict[str, Any]] = []
    for forecast in forecasts:
        agent_id = forecast["agent_id"]
        rows = by_agent.get(agent_id, [])
        windows: dict[str, Any] = {}
        for days in config.windows:
            cutoff = now - timedelta(days=days)
            window_rows = []
            for row in rows:
                try:
                    recorded = datetime.fromisoformat(row["recorded_at"])
                except ValueError:
                    continue
                if recorded.tzinfo is None:
                    recorded = recorded.replace(tzinfo=UTC)
                if recorded >= cutoff:
                    window_rows.append(row)
            windows[f"{days}d"] = _metrics(window_rows, bucket_width=config.bucket_width)
        reports.append(
            {
                "agent_id": agent_id,
                "forecasts": int(forecast["n"]),
                "unresolved": int(forecast["n"]) - len(rows),
                **_metrics(rows, bucket_width=config.bucket_width),
                "windows": windows,
            }
        )
    reports.sort(
        key=lambda item: (
            item["brier"] is None,
            item["brier"] if item["brier"] is not None else 1,
            -item["sample_size"],
        )
    )
    return reports
