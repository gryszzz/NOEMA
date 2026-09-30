"""Read-only market-data qualification projection for the live workstation."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .paired_evaluation import compare_history_to_market
from .paper_execution import parse_aware_time
from .research_worker import market_data_quality


def _historical_corpus(path: str) -> dict[str, Any]:
    result = {
        "status": "unavailable",
        "forecast_records": None,
        "outcome_records": None,
        "forecasts_by_model": None,
        "forecast_outcome_joins_by_model": None,
        "scope": "All persisted forecast_ledger and outcomes rows; not restricted to paired-evaluation eligibility.",
    }
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            tables = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if {"forecast_ledger", "outcomes"}.issubset(tables):
                forecast_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(forecast_ledger)")}
                outcome_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(outcomes)")}
                if not {"forecast_json", "venue", "market_id"}.issubset(forecast_columns) or not {"venue", "market_id"}.issubset(outcome_columns):
                    result["status"] = "incomplete_schema"
                    return result
                result.update(
                    status="recorded",
                    forecast_records=int(conn.execute("SELECT COUNT(*) FROM forecast_ledger").fetchone()[0]),
                    outcome_records=int(conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]),
                    forecasts_by_model={str(model): int(count) for model, count in conn.execute(
                        "SELECT COALESCE(json_extract(forecast_json,'$.model_version'),'unknown'),COUNT(*) "
                        "FROM forecast_ledger GROUP BY 1"
                    )},
                    forecast_outcome_joins_by_model={str(model): int(count) for model, count in conn.execute(
                        "SELECT COALESCE(json_extract(f.forecast_json,'$.model_version'),'unknown'),COUNT(*) "
                        "FROM forecast_ledger f JOIN outcomes o ON f.venue=o.venue AND f.market_id=o.market_id "
                        "GROUP BY 1"
                    )},
                )
            else:
                result["status"] = "incomplete_schema"
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass
    return result


def build_market_data_qualification(
    path: str = "data/noema.db", *, now: datetime | None = None,
) -> dict[str, Any]:
    """Separate live data readiness from strategy validation and execution authority."""
    now = now or datetime.now(UTC)
    historical_corpus = _historical_corpus(path)
    try:
        quality = market_data_quality(path)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {"status": "unavailable", "stage": "insufficient",
                "market_data": {"freshness": "unknown"},
                "historical_corpus": historical_corpus,
                "validation": {"status": "unavailable", "distinct_resolved_events": 0,
                               "minimum_events": 100},
                "execution": {"status": "not_yet_qualified", "live_eligible": False,
                              "authority_granted": False},
                "source": "market snapshot store unavailable"}
    mission_evidence = None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT id,created_at,status,result_json FROM autonomous_research_runs "
                "WHERE kind='market_data_quality' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            payload = json.loads(row[3] or "{}")
            if isinstance(payload, dict):
                mission_evidence = {
                    "run_id": row[0], "created_at": row[1], "run_status": row[2],
                    "status": payload.get("status"),
                    "observations": payload.get("observations"),
                    "valid_markets": payload.get("valid_markets"),
                    "markets_with_rules": payload.get("markets_with_rules"),
                    "markets_with_two_sided_quotes": payload.get("markets_with_two_sided_quotes"),
                    "live_eligible": payload.get("live_eligible"),
                }
    except (OSError, sqlite3.Error, ValueError, TypeError):
        mission_evidence = None
    validation = compare_history_to_market(path).as_dict()
    observations = int(quality.get("observations") or 0)
    valid = int(quality.get("valid_markets") or 0)
    rules = int(quality.get("markets_with_rules") or 0)
    quotes = int(quality.get("markets_with_two_sided_quotes") or 0)
    newest = parse_aware_time(quality.get("newest_snapshot_at")) if quality.get("newest_snapshot_at") else None
    age = max(0.0, (now - newest).total_seconds()) if newest else None
    freshness = "fresh" if age is not None and age <= 300 else "stale" if age is not None else "unknown"

    mission_qualified = bool(
        mission_evidence
        and mission_evidence.get("run_status") == "completed"
        and mission_evidence.get("status") == "research_only"
        and mission_evidence.get("live_eligible") is False
        and type(mission_evidence.get("observations")) is int
        and mission_evidence["observations"] > 0
        and all(type(mission_evidence.get(key)) is int
                and mission_evidence[key] == mission_evidence["observations"]
                for key in ("valid_markets", "markets_with_rules", "markets_with_two_sided_quotes"))
    )
    coverage_qualified = observations > 0 and valid == rules == quotes == observations
    if mission_qualified or coverage_qualified:
        if (int(validation.get("distinct_resolved_events") or 0) >= 100
                and validation.get("status") in {"research_review_required", "candidate_not_better"}):
            stage = "sufficient_for_validation"
        else:
            stage = "sufficient_for_research"
    elif observations > 0:
        stage = "collecting"
    else:
        stage = "insufficient"

    return {
        "as_of": now.astimezone(UTC).isoformat(),
        "stage": stage,
        "market_data": {
            "status": quality.get("status"),
            "observations": observations,
            "valid_markets": valid,
            "markets_with_rules": rules,
            "markets_with_two_sided_quotes": quotes,
            "newest_snapshot_at": quality.get("newest_snapshot_at"),
            "freshness": freshness,
            "age_seconds": age,
            "current_collection": "complete" if observations and valid == rules == quotes == observations else "collecting" if observations else "insufficient",
            "criteria": [
                "Every latest market snapshot passes validation",
                "Every latest market snapshot includes resolution rules",
                "Every latest market snapshot includes a two-sided quote",
                "Newest observed snapshot is no more than 5 minutes old",
            ],
        },
        "recorded_research_evidence": mission_evidence,
        "historical_corpus": historical_corpus,
        "explanation": (
            "A completed, research-only quality mission recorded full validation, resolution-rule, "
            "and two-sided-quote coverage. Current feed freshness and rolling coverage are reported "
            "separately and still gate execution."
            if mission_qualified else
            "Qualification reflects measured latest-snapshot validity, resolution-rule coverage, "
            "two-sided quotes, and chronological outcome evidence."
        ),
        "validation": {
            "model_version": validation.get("model_version"),
            "status": validation.get("status"),
            "distinct_resolved_markets": validation.get("distinct_resolved_markets", 0),
            "distinct_resolved_events": validation.get("distinct_resolved_events", 0),
            "minimum_events": 100,
            "candidate_brier": validation.get("candidate_brier"),
            "market_baseline_brier": validation.get("market_baseline_brier"),
            "live_eligible": False,
        },
        "execution": {
            "status": "not_yet_qualified",
            "live_eligible": False,
            "authority_granted": False,
        },
        "source": "current market snapshot quality + chronological paired evaluation",
    }
