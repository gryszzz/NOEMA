from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .paper_execution import parse_aware_time
from .trench_collector import DEFAULT_HORIZONS, horizon_lateness_seconds
from .trench_models import LaunchTick, TokenControlState
from .trench_survival_model import (
    MIN_TEST_LABELS,
    MIN_TRAIN_LABELS,
    ONE_HOUR_SECONDS,
    _fingerprint,
    load_verified_examples,
)


def _read_only(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    return sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)


def _empty_counts() -> dict[str, int]:
    return {"launches": 0, "observations": 0, "attempts": 0}


def _runtime_state(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    if "agent_runtime" not in tables:
        return {"status": "unavailable", "detail": "No agent runtime state is persisted."}
    try:
        row = conn.execute(
            "SELECT status_json FROM agent_runtime WHERE agent_id='noema'"
        ).fetchone()
        if row is None:
            return {"status": "unavailable", "detail": "No agent runtime state is persisted."}
        status = json.loads(str(row[0]))
        cycle = status.get("last_cycle") or {}
        trench = cycle.get("trench") or {}
        return {
            "status": str(trench.get("status", "unknown")),
            "detail": str(trench.get("detail") or "No Trench status detail is available."),
            "cycle_id": cycle.get("cycle_id"),
            "completed_at": cycle.get("completed_at"),
            "heartbeat_at": status.get("last_heartbeat_at"),
            "running": status.get("running") is True,
        }
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return {"status": "invalid", "detail": "Persisted Trench runtime state is invalid."}


def _progress(
    conn: sqlite3.Connection,
    tables: set[str],
    path: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    attempt_statuses: dict[str, int] = {}
    attempts = int(conn.execute("SELECT COUNT(*) FROM trench_collection_attempts").fetchone()[0]) if "trench_collection_attempts" in tables else 0
    attempt_statuses = ({str(state): int(count) for state, count in conn.execute(
        "SELECT status,COUNT(*) FROM trench_collection_attempts GROUP BY status"
    )} if "trench_collection_attempts" in tables else {})
    if "trench_collection_attempts" in tables:
        missed_query = (
            "SELECT COUNT(*) FROM (SELECT DISTINCT a.mint,a.horizon_seconds "
            "FROM trench_collection_attempts a WHERE a.status='missed'"
        )
        if "trench_observations" in tables:
            missed_query += (
                " AND NOT EXISTS (SELECT 1 FROM trench_observations o "
                "WHERE o.mint=a.mint AND o.horizon_seconds=a.horizon_seconds)"
            )
        missed_query += ")"
        attempt_statuses["missed"] = int(conn.execute(missed_query).fetchone()[0])
    latest_failure = None
    latest_rpc_failure = None
    latest_rpc_detail = None
    latest_rpc_at = None
    if "trench_collection_attempts" in tables:
        failure = conn.execute(
            "SELECT detail FROM trench_collection_attempts "
            "WHERE status IN ('error','unavailable') OR (status='missed' AND NOT EXISTS ("
            "SELECT 1 FROM trench_observations o WHERE o.mint=trench_collection_attempts.mint "
            "AND o.horizon_seconds=trench_collection_attempts.horizon_seconds)) "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_failure = str(failure[0]) if failure and failure[0] else None
        rpc_failure = conn.execute(
            "SELECT attempted_at,detail FROM trench_collection_attempts WHERE detail LIKE "
            "'holder enrichment unavailable:%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest_rpc_failure = str(rpc_failure[1]) if rpc_failure and rpc_failure[1] else None
    observation_rows_total = int(conn.execute("SELECT COUNT(*) FROM trench_observations").fetchone()[0]) if "trench_observations" in tables else None
    qualification_store_present = {"trench_launches", "trench_observations", "trench_candidates", "trench_counterfactuals"} <= tables

    examples = load_verified_examples(connection=conn, now=now)
    matured = len(examples)
    matured_label_checkpoint: list[dict[str, Any]] = []
    if qualification_store_present and examples:
        persisted = conn.execute(
            """SELECT c.candidate_id,c.token_mint,o.scheduled_at,o.observed_at,
                      o.tick_json,cf.observed_at,o.provider_provenance_json
               FROM trench_candidates c
               JOIN trench_observations o ON o.mint=c.token_mint AND o.horizon_seconds=3600
               JOIN trench_counterfactuals cf ON cf.candidate_id=c.candidate_id
                    AND cf.horizon_seconds=3600
               ORDER BY julianday(c.captured_at),c.candidate_id"""
        ).fetchall()
        rows_by_id = {str(row[0]): row for row in persisted}
        chronological = sorted(examples, key=lambda row: (row.captured_at, row.candidate_id))
        for position, example in enumerate(chronological[:5], start=1):
            row = rows_by_id.get(example.candidate_id)
            if row is None or str(row[3]) != str(row[5]):
                continue
            try:
                final_tick = json.loads(str(row[4]))
                provenance = json.loads(str(row[6])) if row[6] else None
                forward_price = float(final_tick["price_usd"])
                if not math.isfinite(forward_price) or forward_price <= 0:
                    continue
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                continue
            matured_label_checkpoint.append({
                "candidate_id": example.candidate_id,
                "launch_identity": str(row[1]),
                "chronological_position": position,
                "cohort_assignment": "training" if position <= MIN_TRAIN_LABELS else "walk_forward",
                "scheduled_maturity_at": str(row[2]),
                "actual_maturity_at": str(row[3]),
                "forward_price_usd": forward_price,
                "label": example.survived,
                "validity": "verified_forward_label",
                "provider_provenance": provenance or {"status": "legacy_source_unattributed"},
            })
    training = min(matured, MIN_TRAIN_LABELS)
    walk_forward_labels_available = min(max(0, matured - MIN_TRAIN_LABELS), MIN_TEST_LABELS)
    cached_audit = None
    if "trench_survival_audits" in tables:
        try:
            cached = conn.execute(
                "SELECT payload_json FROM trench_survival_audits WHERE fingerprint=?",
                (_fingerprint(examples),),
            ).fetchone()
            if cached:
                payload = json.loads(str(cached[0]))
                cached_audit = payload if isinstance(payload, dict) else None
        except (sqlite3.Error, ValueError, TypeError):
            cached_audit = None
    walk_forward_labels = (
        int(cached_audit["walk_forward_tests"])
        if cached_audit and isinstance(cached_audit.get("walk_forward_tests"), int)
        else None
    )
    remaining = max(0, MIN_TRAIN_LABELS + MIN_TEST_LABELS - matured)

    valid_rows = 0
    valid_mints: set[str] = set()
    valid_observation_keys: set[tuple[str, int]] = set()
    invalid_observations = None
    if {"trench_observations", "trench_launches"} <= tables:
        invalid_observations = 0
        for mint, horizon_raw, scheduled_raw, observed_raw, tick_raw, control_raw, first_pool_raw in conn.execute(
            "SELECT o.mint,o.horizon_seconds,o.scheduled_at,o.observed_at,o.tick_json,o.control_json,l.first_pool_at "
            "FROM trench_observations o JOIN trench_launches l ON l.mint=o.mint"
        ):
            try:
                horizon = int(horizon_raw)
                first_pool = parse_aware_time(str(first_pool_raw)).astimezone(UTC)
                scheduled = parse_aware_time(str(scheduled_raw)).astimezone(UTC)
                observed = parse_aware_time(str(observed_raw)).astimezone(UTC)
                if (horizon not in DEFAULT_HORIZONS or scheduled != first_pool + timedelta(seconds=horizon)
                        or observed < scheduled or (observed - scheduled).total_seconds() > horizon_lateness_seconds(horizon)
                        or observed > now + timedelta(seconds=5)):
                    raise ValueError("invalid timing")
                tick_data, control_data = json.loads(str(tick_raw)), json.loads(str(control_raw))
                if not isinstance(tick_data, dict) or not isinstance(control_data, dict):
                    raise TypeError("invalid payload")
                tick_data["observed_at"] = datetime.fromisoformat(str(tick_data["observed_at"]))
                tick_data["holder_shares"] = tuple(float(value) for value in tick_data.get("holder_shares", ()))
                tick = LaunchTick(**tick_data)
                TokenControlState(**control_data)
                if (tick.observed_at.astimezone(UTC) != observed
                        or not all(math.isfinite(float(value)) for value in tick.__dict__.values()
                                   if isinstance(value, (int, float)) and not isinstance(value, bool))):
                    raise ValueError("invalid normalized values")
                valid_rows += 1
                valid_mints.add(str(mint))
                valid_observation_keys.add((str(mint), horizon))
            except (ValueError, TypeError, KeyError, OverflowError, json.JSONDecodeError):
                invalid_observations += 1
    if observation_rows_total is not None and invalid_observations is not None:
        invalid_observations = max(0, observation_rows_total - valid_rows)

    pending = 0
    next_maturation_at = None
    next_maturation_status = "no_pending_candidates"
    if {"trench_candidates", "trench_observations", "trench_counterfactuals"} <= tables:
        mature_ids = {row.candidate_id for row in examples}
        rows = conn.execute(
            """WITH first_candidates AS (
                 SELECT candidate_id,token_mint,captured_at,ROW_NUMBER() OVER (
                   PARTITION BY token_mint ORDER BY julianday(captured_at),candidate_id) AS n
               FROM trench_candidates)
               SELECT c.candidate_id,c.token_mint,c.captured_at,five.scheduled_at,five.observed_at FROM first_candidates c
               JOIN trench_observations five ON five.mint=c.token_mint AND five.horizon_seconds=300
               WHERE c.n=1 AND NOT EXISTS (SELECT 1 FROM trench_counterfactuals cf
                 WHERE cf.candidate_id=c.candidate_id AND cf.horizon_seconds=3600)
               ORDER BY five.scheduled_at"""
        ).fetchall()
        future_targets = []
        for candidate_id, mint, captured_raw, feature_scheduled, feature_observed in rows:
            if str(candidate_id) in mature_ids:
                continue
            try:
                if ((str(mint), 300) not in valid_observation_keys
                        or parse_aware_time(str(captured_raw)).astimezone(UTC)
                        != parse_aware_time(str(feature_observed)).astimezone(UTC)):
                    continue
                target = parse_aware_time(str(feature_scheduled)).astimezone(UTC) + timedelta(seconds=ONE_HOUR_SECONDS - 300)
            except (ValueError, TypeError):
                continue
            pending += 1
            if target > now:
                future_targets.append(target)
        if future_targets:
            next_maturation_at = min(future_targets).isoformat()
            next_maturation_status = "earliest_scheduled_outcome"
        elif pending:
            next_maturation_status = "due_or_overdue_waiting_for_collection"

    runtime = _runtime_state(conn, tables)
    latest_field_provenance = None
    if "trench_observations" in tables:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(trench_observations)")}
        if "provider_provenance_json" in columns:
            source = conn.execute(
                "SELECT provider_provenance_json FROM trench_observations "
                "WHERE provider_provenance_json IS NOT NULL ORDER BY observed_at DESC LIMIT 1"
            ).fetchone()
            if source and source[0]:
                try:
                    latest_field_provenance = json.loads(str(source[0]))
                except (json.JSONDecodeError, TypeError):
                    latest_field_provenance = {"status": "invalid_provenance_record"}
        if latest_field_provenance is None and observation_rows_total:
            latest_field_provenance = {"status": "legacy_source_unattributed"}
    last_success = None
    rpc_success = None
    rpc_failed = False
    if "trench_collection_attempts" in tables:
        last_success = conn.execute("SELECT MAX(attempted_at) FROM trench_collection_attempts WHERE status='recorded'").fetchone()[0]
        rpc_attempt = conn.execute(
            "SELECT attempted_at,detail FROM trench_collection_attempts "
            "WHERE detail LIKE '%holder enrichment recorded%' OR detail LIKE '%holder enrichment unavailable%' "
            "ORDER BY attempted_at DESC LIMIT 1"
        ).fetchone()
        rpc_success = (rpc_attempt[0] if rpc_attempt
                       and str(rpc_attempt[1]).startswith("holder enrichment recorded") else None)
        rpc_failed = bool(rpc_attempt and "holder enrichment unavailable" in str(rpc_attempt[1]))
        latest_rpc_at = rpc_attempt[0] if rpc_attempt else None
        latest_rpc_detail = str(rpc_attempt[1]) if rpc_attempt else None

    def health(at: object, success: bool) -> str:
        if not at:
            return "unknown"
        try:
            age = (now - parse_aware_time(str(at)).astimezone(UTC)).total_seconds()
        except (TypeError, ValueError):
            return "unknown"
        return "stale" if age < 0 or age > 1800 else "healthy" if success else "degraded"

    allocation = None
    if "ecosystem_reviews" in tables:
        try:
            row = conn.execute("SELECT payload_json FROM ecosystem_reviews ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                for item in json.loads(str(row[0])).get("allocations", []):
                    if isinstance(item, dict) and item.get("specialist") == "trench-1":
                        candidate = item.get("attention_fraction")
                        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and 0 <= candidate <= 1:
                            allocation = float(candidate)
                        break
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            pass
    trial_status = None
    if "research_trials" in tables:
        trial = conn.execute("SELECT status FROM research_trials WHERE family='trench_survival' ORDER BY created_at DESC LIMIT 1").fetchone()
        trial_status = str(trial[0]) if trial else None

    trench_flag = os.getenv("NOEMA_TRENCH_ENABLED")
    collector_enabled = (None if trench_flag is None else
                         trench_flag.strip().lower() in {"1", "true", "yes", "on"})
    research_flag = os.getenv("NOEMA_RESEARCH_WORK_ENABLED")
    research_enabled = (None if research_flag is None else
                        research_flag.strip().lower() in {"1", "true", "yes", "on"})
    heartbeat = runtime.get("heartbeat_at")
    try:
        runtime_fresh = (bool(heartbeat) and 0 <=
                         (now - parse_aware_time(str(heartbeat)).astimezone(UTC)).total_seconds() <= 90)
    except (TypeError, ValueError):
        runtime_fresh = False
    persisted_providers: dict[str, dict[str, Any]] = {}
    if "trench_provider_health" in tables:
        persisted_providers = {str(row[0]): {
            "state": str(row[1]), "last_attempt_at": row[2], "last_success_at": row[3],
            "last_failure_at": row[4], "consecutive_failures": int(row[5]),
            "next_retry_at": row[6], "last_error_class": row[7],
        } for row in conn.execute(
            "SELECT provider,state,last_attempt_at,last_success_at,last_failure_at,consecutive_failures,next_retry_at,last_error_class FROM trench_provider_health"
        )}
    maturity_schedule: dict[str, int] = {}
    maturity_rows: list[dict[str, Any]] = []
    if "trench_candidate_maturities" in tables:
        maturity_schedule = {str(row[0]): int(row[1]) for row in conn.execute(
            "SELECT state,COUNT(*) FROM trench_candidate_maturities GROUP BY state"
        )}
        maturity_rows = [{
            "candidate_id": str(row[0]), "target_at": str(row[1]),
            "state": str(row[2]), "attempts": int(row[3]),
            "last_attempt_at": row[4], "next_retry_at": row[5],
            "detail": row[6],
        } for row in conn.execute(
            "SELECT candidate_id,target_at,state,attempts,last_attempt_at,next_retry_at,detail FROM trench_candidate_maturities ORDER BY target_at LIMIT 25"
        )]

    def provider_summary(keys: list[str]) -> dict[str, Any]:
        rows = [persisted_providers[key] for key in keys if key in persisted_providers]
        if not rows:
            return {"state": "unknown", "last_success_at": None,
                    "last_failure_at": None, "next_retry_at": None,
                    "last_error_class": None}
        latest = max(rows, key=lambda item: str(item.get("last_attempt_at") or ""))
        failed_after_success = (latest.get("last_failure_at") is not None and
                               (latest.get("last_success_at") is None or
                                str(latest["last_failure_at"]) > str(latest["last_success_at"])))
        return {"state": "degraded" if failed_after_success else latest["state"],
                "last_success_at": latest.get("last_success_at"),
                "last_failure_at": latest.get("last_failure_at"),
                "next_retry_at": latest.get("next_retry_at"),
                "last_error_class": latest.get("last_error_class")}

    jupiter_provider_state = provider_summary(
        ["jupiter_price", "jupiter_market", "jupiter_discovery"]
    )
    solana_provider_state = provider_summary([
        provider for provider in persisted_providers if provider.startswith("solana_rpc:")
    ])
    runtime_detail = str(runtime.get("detail") or "")
    jupiter_degraded = (jupiter_provider_state["state"] == "degraded"
                        if persisted_providers else runtime_fresh and "jupiter:" in runtime_detail)
    solana_health = (solana_provider_state["state"] if persisted_providers else
                     "degraded" if rpc_failed and latest_rpc_at
                     else health(latest_rpc_at, bool(rpc_success)))
    collector_status = (runtime["status"] if collector_enabled is True else
                        "disabled" if collector_enabled is False else "unknown_configuration")
    if collector_enabled is True and not runtime.get("running"):
        collector_status = "stopped"
    elif collector_enabled is True and not runtime_fresh:
        collector_status = "stale" if heartbeat else "unknown"
    evidence_ready = remaining == 0
    mission_eligible = (evidence_ready and research_enabled is True and allocation is not None
                        and allocation > 0 and runtime_fresh and runtime.get("running"))
    if not evidence_ready:
        mission_state = "collecting_forward_evidence"
    elif research_enabled is not True:
        mission_state = ("threshold_satisfied_research_work_disabled" if research_enabled is False
                         else "threshold_satisfied_research_authority_unknown")
    elif allocation is None or allocation <= 0:
        mission_state = "threshold_satisfied_waiting_for_positive_attention"
    elif not runtime_fresh:
        mission_state = "threshold_satisfied_runtime_needs_attention"
    elif trial_status:
        mission_state = f"existing_trial_{trial_status}"
    else:
        mission_state = "eligible_existing_research_path"

    return {
        "observations_collected": valid_rows if observation_rows_total is not None else None,
        "valid_observations": valid_rows if observation_rows_total is not None else None,
        "invalid_observations": invalid_observations,
        "rejected_attempts": int(attempt_statuses.get("error", 0)) + int(attempt_statuses.get("missed", 0)),
        "failed_attempts": int(attempt_statuses.get("error", 0)),
        "missed_attempts": int(attempt_statuses.get("missed", 0)),
        "unavailable_attempts": int(attempt_statuses.get("unavailable", 0)),
        "launch_distinct_observations": len(valid_mints) if observation_rows_total is not None else None,
        "labels_pending": pending if {"trench_candidates", "trench_observations", "trench_counterfactuals"} <= tables else None,
        "labels_matured": matured if qualification_store_present else None,
        "matured_label_checkpoint": matured_label_checkpoint if qualification_store_present else None,
        "training_labels": training if qualification_store_present else None,
        "walk_forward_labels": walk_forward_labels if qualification_store_present else None,
        "threshold": {"training": MIN_TRAIN_LABELS, "walk_forward": MIN_TEST_LABELS},
        "threshold_remaining": ({"training": max(0, MIN_TRAIN_LABELS - training),
                                 "walk_forward": max(0, MIN_TEST_LABELS - walk_forward_labels_available),
                                 "total": remaining} if qualification_store_present else
                                {"training": None, "walk_forward": None, "total": None}),
        "walk_forward_labels_available": walk_forward_labels_available if qualification_store_present else None,
        "next_maturation_at": next_maturation_at,
        "next_maturation_status": next_maturation_status if qualification_store_present else "unknown",
        "last_successful_collection": last_success,
        "collector_enabled": collector_enabled,
        "collector_status": collector_status,
        "provider_health": {
            "jupiter": "degraded" if jupiter_degraded else health(last_success, bool(last_success)),
            "solana_rpc": solana_health,
            "records": persisted_providers,
            "jupiter_detail": jupiter_provider_state,
            "solana_rpc_detail": solana_provider_state,
        },
        "maturity_schedule": {
            "states": maturity_schedule,
            "candidates": maturity_rows,
        },
        "solana_rpc_last_enrichment_at": rpc_success,
        "solana_rpc_last_attempt_at": latest_rpc_at,
        "solana_rpc_fallback_configured": bool(
            os.getenv("NOEMA_SOLANA_RPC_FALLBACK_URL", "").strip()
        ),
        "latest_field_provenance": latest_field_provenance,
        "solana_rpc_latest_attempt": latest_rpc_detail,
        "mission_eligible": mission_eligible if qualification_store_present else False,
        "eligibility_reason": mission_state.replace("_", " "),
        "research_work_enabled": research_enabled,
        "allocated_attention_fraction": allocation,
        "trial_status": trial_status,
        "next_eligible_mission_state": mission_state,
        "audit_status": (cached_audit.get("status") if cached_audit else
                         "unknown" if not qualification_store_present else
                         "awaiting_existing_survival_audit" if remaining == 0 else
                         "insufficient_forward_labels"),
        "survival_model": cached_audit,
        "walk_forward_scored": walk_forward_labels,
        "attempts": attempts,
        "attempt_statuses": attempt_statuses,
        "latest_failure": latest_failure,
        "latest_rpc_failure": latest_rpc_failure,
        "collection_mode": "read_only",
        "wallet_authority": "none_from_research_capability",
        "runtime": runtime,
    }


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
            "counts": _empty_counts(),
            "progress": None,
            "recent_launches": [],
            "recent_candidates": [],
            "survival_model": None,
        }

    conn = _read_only(path)
    try:
        tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        launches = []
        if "trench_launches" in tables:
            launches = conn.execute(
                """
                SELECT mint, first_pool_at, discovered_at, last_seen_at, active
                FROM trench_launches ORDER BY first_pool_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        candidates = []
        if "trench_candidates" in tables:
            candidates = conn.execute(
                """
                SELECT token_mint, captured_at, disposition, survival_risk,
                       opportunity_score, assessment_json
                FROM trench_candidates ORDER BY captured_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        counts = {
            "launches": int(conn.execute("SELECT COUNT(*) FROM trench_launches").fetchone()[0])
            if "trench_launches" in tables else 0,
            "observations": int(conn.execute("SELECT COUNT(*) FROM trench_observations").fetchone()[0])
            if "trench_observations" in tables else 0,
            "attempts": int(conn.execute("SELECT COUNT(*) FROM trench_collection_attempts").fetchone()[0])
            if "trench_collection_attempts" in tables else 0,
        }
        progress = _progress(conn, tables, path)
    finally:
        conn.close()

    return {
        "database_present": True,
        "counts": counts,
        "progress": progress,
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
        "survival_model": progress["survival_model"],
        "recent_candidates": [
            {
                "mint": str(mint),
                "captured_at": str(captured_at),
                "disposition": str(disposition),
                "survival_risk": float(survival_risk),
                "opportunity_score": float(opportunity_score),
                "assessment": json.loads(str(assessment_json)),
            }
            for mint, captured_at, disposition, survival_risk, opportunity_score, assessment_json in candidates
        ],
    }
