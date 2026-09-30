"""Bounded, read-only operational projection. Never constructs a writable Store."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .openclaw_worker import runtime_status as openclaw_runtime_status
from .paper_execution import parse_aware_time
from .research_state import (
    merge_research_run_records,
    research_run_identity,
    research_trial_update_is_newer,
)
from .resource_control import resource_status
from .runtime_diagnostics import diagnose_runtime

# Identifiers below are application constants, never request parameters.
SECTIONS = {
    "sessions": ("cognitive_sessions", ("session_id,identity_hash,created_at,completed_at,status,"
                 "objective,provider,model,input_tokens,output_tokens,cached_tokens,"
                 "estimated_model_cost_usd,compute_cost_usd,result_json"), "created_at"),
    "activity": ("runtime_events", ("id,session_id,created_at,stage,status,tool,evidence_id,"
                 "elapsed_seconds,cost_usd,detail"), "id"),
    "lessons": ("research_lessons", ("id,mission_id,session_id,trial_id,created_at,next_priority,"
                "evidence_hash,lesson"), "id"),
    "research_runs": ("autonomous_research_runs", ("id,trial_id,specialist,kind,evidence_hash,"
                      "worker_version,status,created_at,completed_at,elapsed_seconds,"
                      "compute_cost_usd,result_json,evidence_path,mission_id"), "id"),
    "missions": ("missions", ("mission_id,trial_id,evidence_hash,session_id,run_id,objective,status,"
                    "specialist,capability_grants_json,resource_grant_json,result_json,lesson_id,"
                    "created_at,updated_at,completed_at"), "updated_at"),
    "mission_events": ("mission_events", ("id,mission_id,created_at,actor,event_type,status,detail,"
                          "payload_json"), "id"),
    "wallet_transactions": ("economic_events", ("id,created_at,event_type,amount_usd,payload_json"), "id"),
    "economic_events": ("economic_events", ("id,created_at,occurred_at,provider,event_type,"
                        "amount_usd,reconciliation_state,capital_class,mission_id,"
                        "strategy_id,lane"), "id"),
    "handoffs": ("mission_handoffs", ("handoff_id,mission_id,created_at,updated_at,from_specialist,"
                      "to_specialist,objective,status,capability_grants_json,resource_grant_json,"
                      "result_json"), "updated_at"),
    "queue": ("research_queue", "task_id,market_id,request,priority,status,created_at", "created_at"),
    "specialists": ("ecosystem_specialists", ("name,family,state,resolved,reliability,"
                    "calibration_error,after_cost_return,drawdown_fraction,updated_at"), "updated_at"),
    "experiments": ("research_trials", ("trial_id,family,hypothesis,feature_set_version,status,"
                    "created_at,parent_trial_id"), "created_at"),
    "decisions": ("forecast_ledger", "id,created_at,venue,market_id,forecast_json,action_json", "id"),
    "reviews": ("specialist_evolution_reviews", "id,specialist,created_at,previous_state,next_state",
                "id"),
    "reservations": ("cognition_budget_reservations", ("id,day_utc,estimated_usd,created_at,"
                     "estimated_tokens,market_id"), "id"),
    "outcomes": ("outcomes", "venue,market_id,outcome_yes,resolved_at,first_seen_at", "first_seen_at"),
}

# Stable economic desks. Family/kind matching only classifies recorded work; it
# never turns work, forecasts, or internal budgets into revenue.
ECONOMIC_LANES = (
    ("web3", "Web3 / on-chain", ("web3", "solana", "onchain", "on_chain", "evm", "crypto")),
    ("prediction", "Prediction markets", ("prediction", "kalshi", "polymarket", "forecast")),
    ("saas", "SaaS / micro-SaaS", ("saas", "micro_saas", "software_service")),
    ("apis", "APIs", ("api", "apis")),
    ("data", "Data products", ("data_product", "data_products")),
    ("subscriptions", "Subscriptions", ("subscription", "subscriptions")),
    ("automation", "Automation", ("automation", "workflow")),
    ("research", "Research / intelligence", ("research_product", "intelligence_product")),
    ("agent_services", "Agent services", ("agent_service", "agent_services")),
    ("other", "Other internet-native", ("other", "internet_native")),
)


def _experiment_relationship_counts(
    conn: sqlite3.Connection, tables: set[str], record: dict[str, Any],
    params: dict[str, Any], *, decision_conn: sqlite3.Connection | None = None,
    decision_tables: set[str] | None = None,
) -> dict[str, Any]:
    """Attach counts using persisted trial/evidence/decision identities only."""
    counts: dict[str, Any] = {
        "run_count": None, "evidence_count": None, "decision_count": None,
        "strategy_decision_count": None, "relationship_basis": "persisted identifiers",
    }
    trial_id = record.get("trial_id")
    if "autonomous_research_runs" in tables and trial_id is not None:
        try:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(autonomous_research_runs)"
            )}
            if "trial_id" in columns:
                counts["run_count"] = conn.execute(
                    "SELECT COUNT(*) FROM autonomous_research_runs WHERE trial_id=?", (trial_id,),
                ).fetchone()[0]
                evidence_cols = [key for key in ("evidence_hash", "evidence_path") if key in columns]
                if evidence_cols:
                    predicate = " OR ".join(f"COALESCE({key},'')!=''" for key in evidence_cols)
                    counts["evidence_count"] = conn.execute(
                        f"SELECT COUNT(*) FROM autonomous_research_runs WHERE trial_id=? AND ({predicate})",
                        (trial_id,),
                    ).fetchone()[0]
        except sqlite3.Error:
            pass
    forecast_tables = tables if decision_tables is None else decision_tables
    forecast_conn = decision_conn or conn
    if "forecast_ledger" not in forecast_tables:
        return counts
    strategy_id = record.get("strategy_id")
    try:
        columns = {row[1] for row in forecast_conn.execute("PRAGMA table_info(forecast_ledger)")}
        if not {"forecast_json"} <= columns:
            return counts
        clauses: list[str] = []
        values: list[Any] = []
        if trial_id is not None:
            clauses.append("CASE WHEN json_valid(forecast_json) THEN json_extract(forecast_json,'$.trial_id') END=?")
            values.append(trial_id)
        if strategy_id is not None:
            clauses.append("CASE WHEN json_valid(forecast_json) THEN json_extract(forecast_json,'$.strategy_id') END=?")
            values.append(strategy_id)
        if clauses:
            counts["decision_count"] = forecast_conn.execute(
                "SELECT COUNT(*) FROM forecast_ledger WHERE json_valid(forecast_json) "
                "AND (" + " OR ".join(clauses) + ")", tuple(values),
            ).fetchone()[0]
        if strategy_id is not None:
            counts["strategy_decision_count"] = forecast_conn.execute(
                "SELECT COUNT(*) FROM forecast_ledger WHERE json_valid(forecast_json) "
                "AND CASE WHEN json_valid(forecast_json) THEN json_extract(forecast_json,'$.strategy_id') END=?", (strategy_id,),
            ).fetchone()[0]
    except sqlite3.Error:
        # JSON1 is optional in some system SQLite builds. Exact fallback keeps
        # linked decisions visible without treating malformed records as matches.
        try:
            rows = forecast_conn.execute("SELECT forecast_json FROM forecast_ledger").fetchall()
            matched = strategy_matches = 0
            for (raw,) in rows:
                try:
                    forecast = _object(raw)
                except (ValueError, TypeError):
                    continue
                by_trial = trial_id is not None and forecast.get("trial_id") == trial_id
                by_strategy = strategy_id is not None and forecast.get("strategy_id") == strategy_id
                matched += bool(by_trial or by_strategy)
                strategy_matches += bool(by_strategy)
            counts["decision_count"] = matched
            if strategy_id is not None:
                counts["strategy_decision_count"] = strategy_matches
        except sqlite3.Error:
            pass
    return counts


def _project_experiment_record(
    row: sqlite3.Row, conn: sqlite3.Connection, tables: set[str], *,
    decision_conn: sqlite3.Connection | None = None, decision_tables: set[str] | None = None,
) -> dict[str, Any]:
    """Canonical trial projection shared by worker and console-sidecar records."""
    values = dict(row)
    record = {key: _scalar(value) for key, value in values.items()
              if not key.endswith("_json")}
    params: dict[str, Any] = {}
    raw_params = values.get("params_json")
    if raw_params:
        try:
            parsed = _object(raw_params)
            params = parsed if isinstance(parsed, dict) else {}
            record["params"] = params
        except (ValueError, TypeError):
            record["record_status"] = "invalid_params"
    raw_metadata = values.get("metadata_json")
    if raw_metadata:
        try:
            metadata = _object(raw_metadata)
            record["experiment_metadata"] = metadata
            params = {**metadata, **params}
        except (ValueError, TypeError):
            record["record_status"] = "invalid_metadata"
    for key in ("strategy_id", "market_id", "venue", "asset", "ticker"):
        value = record.get(key) or params.get(key)
        if value is None and isinstance(params.get("market"), dict):
            value = params["market"].get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            record[key] = str(value)
    evidence_hash = params.get("candidate_observation_hash")
    if isinstance(evidence_hash, str) and "canonical_pair_observations" in tables:
        try:
            evidence = conn.execute(
                "SELECT observation_json FROM canonical_pair_observations "
                "WHERE observation_hash=? LIMIT 1", (evidence_hash,),
            ).fetchone()
            if evidence:
                observation = _object(evidence[0])
                contracts = observation.get("native_contracts")
                market_ids = list(dict.fromkeys(
                    item.get("market_id") for item in contracts
                    if isinstance(item, dict) and isinstance(item.get("market_id"), str)
                )) if isinstance(contracts, list) else []
                if market_ids:
                    record["market_ids"] = market_ids
                    record.setdefault("market_id", market_ids[0])
        except (sqlite3.Error, ValueError, TypeError):
            pass
    record.update(_experiment_relationship_counts(
        conn, tables, record, params, decision_conn=decision_conn,
        decision_tables=decision_tables,
    ))
    return record


def _combined_research_run_counts(
    connections: tuple[sqlite3.Connection, ...], trial_id: str,
) -> dict[str, int | None]:
    """Count deduplicated run/evidence identities across worker and sidecar files."""
    run_keys: set[tuple[Any, ...]] = set()
    evidence_keys: set[tuple[str, str]] = set()
    table_available = False
    for conn in connections:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "autonomous_research_runs" not in tables:
            continue
        table_available = True
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(autonomous_research_runs)")}
        if "trial_id" not in columns:
            continue
        fields = [field for field in (
            "id", "trial_id", "evidence_hash", "evidence_path", "worker_version",
        ) if field in columns]
        for row in conn.execute(
            f"SELECT {','.join(fields)} FROM autonomous_research_runs WHERE trial_id=?",
            (trial_id,),
        ):
            item = dict(zip(fields, row, strict=True))
            digest, version = item.get("evidence_hash"), item.get("worker_version")
            if digest:
                run_keys.add(("evidence", str(digest), str(version or "")))
                evidence_keys.add(("hash", str(digest)))
            else:
                path = item.get("evidence_path")
                if path:
                    run_keys.add(("path", str(path), str(version or "")))
                    evidence_keys.add(("path", str(path)))
                else:
                    run_keys.add(("row", str(item.get("id"))))
    return {
        "run_count": len(run_keys) if table_available else None,
        "evidence_count": len(evidence_keys) if table_available else None,
    }


def _economic_lane_projection(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    """Classify persisted work into desks; financial results remain unmeasured."""
    lanes = {
        key: {"key": key, "name": name, "trial_count": 0, "run_count": 0,
              "specialist_count": 0, "activity_status": "no_recorded_activity",
              "receipts_usd": None, "verified_expenses_usd": None,
              "realized_net_usd": None, "financial_status": "unmeasured"}
        for key, name, _ in ECONOMIC_LANES
    }

    def lane_for(value: object) -> str | None:
        text = str(value or "").lower().replace("-", "_")
        for key, _name, terms in ECONOMIC_LANES:
            if any(term in text for term in terms):
                return key
        return None

    queries = (
        ("research_trials", "family", "trial_count"),
        ("ecosystem_specialists", "family", "specialist_count"),
    )
    for table, column, count_key in queries:
        if table not in tables:
            continue
        try:
            for (value,) in conn.execute(f"SELECT {column} FROM {table} LIMIT 10000"):
                key = lane_for(value)
                if key:
                    lanes[key][count_key] += 1
        except sqlite3.Error:
            continue
    if "autonomous_research_runs" in tables:
        try:
            if "research_trials" in tables:
                run_rows = conn.execute(
                    "SELECT r.kind,r.specialist,t.family FROM autonomous_research_runs r "
                    "LEFT JOIN research_trials t ON t.trial_id=r.trial_id LIMIT 10000"
                )
            else:
                run_rows = conn.execute(
                    "SELECT kind,specialist,NULL FROM autonomous_research_runs LIMIT 10000"
                )
            for kind, specialist, family in run_rows:
                key = lane_for(family) or lane_for(kind) or lane_for(specialist)
                if key:
                    lanes[key]["run_count"] += 1
        except sqlite3.Error:
            pass
    for lane in lanes.values():
        if lane["trial_count"] or lane["run_count"] or lane["specialist_count"]:
            lane["activity_status"] = "recorded_research_or_capability"
    return {
        "status": "recorded" if tables else "unavailable",
        "basis": "Persisted trial families, research-run kinds/specialists, and registered specialist families.",
        "financial_basis": "No lane-level reconciled cash or complete-cost attribution is recorded.",
        "lanes": list(lanes.values()),
    }


def _mission_measurement(conn: sqlite3.Connection, tables: set[str], mission: sqlite3.Row) -> dict[str, Any]:
    """Attribute only values linked to this mission by persisted identifiers.

    This is a projection over existing ledgers: missing or ambiguous costs remain
    null, and operator-reported cash is never represented as verified profit.
    """
    trial_id = str(mission["trial_id"])
    run_id = mission["run_id"]
    session_id = mission["session_id"]
    lane = None
    if "research_trials" in tables:
        try:
            row = conn.execute("SELECT family FROM research_trials WHERE trial_id=?", (trial_id,)).fetchone()
            family = str(row[0]) if row else ""
            normalized = family.lower().replace("-", "_")
            for key, _name, terms in ECONOMIC_LANES:
                if any(term in normalized for term in terms):
                    lane = key
                    break
        except sqlite3.Error:
            pass

    run = None
    if "autonomous_research_runs" in tables and run_id is not None:
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(autonomous_research_runs)")}
            available = [key for key in ("id", "specialist", "kind", "status", "elapsed_seconds",
                                          "compute_cost_usd", "result_json", "mission_id")
                         if key in columns]
            if "id" in available:
                row = conn.execute(
                    f"SELECT {','.join(available)} FROM autonomous_research_runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                run = dict(row) if row else None
        except sqlite3.Error:
            pass

    session = None
    if "cognitive_sessions" in tables and session_id:
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cognitive_sessions)")}
            wanted = ("session_id", "created_at", "completed_at", "provider", "model",
                      "estimated_model_cost_usd", "compute_cost_usd")
            available = [key for key in wanted if key in columns]
            row = conn.execute(
                f"SELECT {','.join(available)} FROM cognitive_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            session = dict(row) if row else None
        except sqlite3.Error:
            pass

    elapsed_session = None
    if session and session.get("created_at") and session.get("completed_at"):
        try:
            elapsed_session = max(0.0, (parse_aware_time(session["completed_at"])
                                        - parse_aware_time(session["created_at"])).total_seconds())
        except (ValueError, TypeError):
            pass

    linked_ids = {str(value) for value in (mission["mission_id"], run_id, session_id)
                  if value is not None}
    cash_receipts = cash_expenses = Decimal(0)
    cash_count = 0
    cash_linked = False
    if "bill_entries" in tables and linked_ids:
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(bill_entries)")}
            if {"activity_id", "kind", "amount_usd"} <= columns:
                placeholders = ",".join("?" for _ in linked_ids)
                for kind, amount in conn.execute(
                    f"SELECT kind,amount_usd FROM bill_entries WHERE activity_id IN ({placeholders})",
                    tuple(sorted(linked_ids)),
                ):
                    try:
                        value = Decimal(str(amount))
                        if not value.is_finite() or value < 0:
                            continue
                    except (InvalidOperation, ValueError, TypeError):
                        continue
                    if kind == "receipt":
                        cash_receipts += value
                    elif kind == "expense":
                        cash_expenses += value
                    else:
                        continue
                    cash_count += 1
                cash_linked = cash_count > 0
        except sqlite3.Error:
            pass

    result = None
    if run and run.get("result_json"):
        try:
            result = _object(run["result_json"])
        except (ValueError, TypeError):
            result = None
    critic = result.get("critic_review") if isinstance(result, dict) else None
    information = None
    if isinstance(result, dict):
        information = {key: result[key] for key in (
            "observations", "valid_markets", "invalid_markets", "unresolved_markets",
            "conclusion", "status", "live_eligible", "public_search_result_count",
            "metadata_records_reviewed", "visible_payment_signal_count", "verified_buyer_count",
            "verified_payout_count", "deliverable_produced", "delivery_tested",
            "revenue_test_status", "mission_cash_receipt_usd", "realized_net_value_usd",
        ) if key in result and isinstance(result[key], (str, int, float, bool, type(None)))}
        if isinstance(result.get("attributable_costs_usd"), dict):
            information["attributable_costs_usd"] = {
                key: _scalar(value) for key, value in result["attributable_costs_usd"].items()
                if key in {"model", "compute", "source_api", "delivery", "support"}
            }
        if isinstance(critic, dict):
            information["critic_verdict"] = critic.get("verdict")
            information["critic_accepted"] = critic.get("result_accepted")

    contributions = []
    lead = str(mission["specialist"])
    if run:
        contributions.append({"specialist": lead, "role": "research worker",
                              "status": run.get("status"),
                              "elapsed_seconds": run.get("elapsed_seconds"), "cost_usd": None})
    elif mission["status"] == "passed" and "mission_events" in tables:
        try:
            decisions = conn.execute(
                "SELECT actor,event_type,status FROM mission_events WHERE mission_id=? "
                "AND event_type='lesson_applied' ORDER BY id", (mission["mission_id"],),
            ).fetchall()
            contributions.extend({"specialist": item[0], "role": "lesson-based coordination",
                                  "status": item[2], "event": item[1],
                                  "elapsed_seconds": None, "cost_usd": None}
                                 for item in decisions)
        except sqlite3.Error:
            pass
    else:
        contributions.append({"specialist": lead, "role": "assigned specialist; no work started",
                              "status": "not_started", "elapsed_seconds": None, "cost_usd": None})
    if session:
        contributions.append({
            "specialist": "NOEMA cognition",
            "role": "mission selection / coordination",
            "status": "recorded",
            "provider": session.get("provider"),
            "model": session.get("model"),
            "cost_usd": session.get("estimated_model_cost_usd"),
            "cost_basis": "provider usage price estimate" if
                session.get("estimated_model_cost_usd") is not None else "unknown",
        })
    if "mission_handoffs" in tables:
        try:
            for item in conn.execute(
                "SELECT to_specialist,status,objective,result_json FROM mission_handoffs "
                "WHERE mission_id=? ORDER BY created_at,id", (mission["mission_id"],),
            ):
                handoff_result = {}
                if item[3]:
                    try:
                        handoff_result = _object(item[3])
                    except (ValueError, TypeError):
                        pass
                contributions.append({"specialist": item[0], "role": "persisted handoff",
                                      "status": item[1], "objective": item[2],
                                      "elapsed_seconds": handoff_result.get("elapsed_seconds"),
                                      "cost_usd": handoff_result.get("cost_usd")})
        except sqlite3.Error:
            pass

    allocation_follow_up: dict[str, Any] = {
        "status": "not_observed", "current_attention_fraction": None,
        "previous_attention_fraction": None, "attention_delta": None,
        "basis": "No post-completion allocation review linked by time is available",
    }
    if "ecosystem_reviews" in tables:
        try:
            reviews = conn.execute(
                "SELECT id,created_at,payload_json FROM ecosystem_reviews ORDER BY id DESC LIMIT 100"
            ).fetchall()
            completed_at = mission["completed_at"]

            exact_review = None
            for review_row in reviews:
                payload = _object(review_row["payload_json"])
                candidate = payload.get("mission_review")
                if isinstance(candidate, dict) and candidate.get("mission_id") == mission["mission_id"]:
                    exact_review = (review_row, candidate)
                    break

            def share_for(payload_text: str, specialist: str) -> float | None:
                payload = _object(payload_text)
                allocations = payload.get("allocations")
                if not isinstance(allocations, list):
                    return None
                for allocation in allocations:
                    if (isinstance(allocation, dict) and allocation.get("specialist") == specialist
                            and isinstance(allocation.get("attention_fraction"), (int, float))
                            and not isinstance(allocation.get("attention_fraction"), bool)
                            and math.isfinite(allocation["attention_fraction"])
                            and 0 <= allocation["attention_fraction"] <= 1):
                        return float(allocation["attention_fraction"])
                return None

            if exact_review:
                row, review = exact_review
                allocation_follow_up = {
                    "status": "observed",
                    "outcome": _scalar(review.get("outcome")),
                    "specialist": _scalar(review.get("specialist")),
                    "review_id": _scalar(review.get("review_id") or row["id"]),
                    "current_attention_fraction": _scalar(review.get("current_attention_fraction")),
                    "previous_attention_fraction": _scalar(review.get("previous_attention_fraction")),
                    "attention_delta": (
                        float(review["current_attention_fraction"])
                        - float(review["previous_attention_fraction"])
                        if isinstance(review.get("current_attention_fraction"), (int, float))
                        and not isinstance(review.get("current_attention_fraction"), bool)
                        and isinstance(review.get("previous_attention_fraction"), (int, float))
                        and not isinstance(review.get("previous_attention_fraction"), bool)
                        and math.isfinite(review["current_attention_fraction"])
                        and math.isfinite(review["previous_attention_fraction"])
                        else None
                    ),
                    "basis": _scalar(review.get("basis")),
                }
            else:
                eligible = [row for row in reviews if completed_at and
                            parse_aware_time(row["created_at"]) >= parse_aware_time(completed_at)]
                if eligible:
                    current = share_for(eligible[0]["payload_json"], lead)
                    previous = None
                    if len(eligible) > 1:
                        previous = share_for(eligible[1]["payload_json"], lead)
                    allocation_follow_up = {
                        "status": "observed" if current is not None else "specialist_not_in_plan",
                        "current_attention_fraction": current,
                        "previous_attention_fraction": previous,
                        "attention_delta": current - previous if current is not None and previous is not None else None,
                        "basis": "Legacy post-completion review; temporal association, not exclusive causation",
                    }
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            allocation_follow_up["status"] = "invalid"

    paper_outcome = None
    if run and run.get("kind") == "market_data_quality":
        paper_outcome = {"status": "not_applicable",
                         "basis": "Data-quality research did not open or settle paper positions"}
    elif run and run.get("kind") == "cost_threshold_sweep":
        variants = result.get("variants") if isinstance(result, dict) else None
        if isinstance(variants, list) and len(variants) <= 32:
            paper_outcome = {
                "status": "recorded" if variants else "insufficient_evidence",
                "basis": "Retrospective hypothetical paper outcomes after execution costs; not cash",
                "variants": [{key: variant.get(key) for key in (
                    "threshold", "markets", "events", "paper_debit_usd", "paper_net_usd",
                    "paper_return",
                )} for variant in variants if isinstance(variant, dict)],
            }
        else:
            paper_outcome = {"status": "unknown"}
    elif run:
        paper_outcome = {"status": "not_applicable",
                         "basis": "This research handler does not represent a paper position"}

    return {
        "economic_lane": lane,
        "model_api_cost_usd": (session.get("estimated_model_cost_usd")
                               if session else None),
        "model_api_cost_basis": "provider usage price estimate" if session and
            session.get("estimated_model_cost_usd") is not None else "unknown",
        "compute_cost_usd": (run.get("compute_cost_usd") if run and
                              run.get("compute_cost_usd") is not None else
                              session.get("compute_cost_usd") if session else None),
        "compute_cost_basis": "recorded" if ((run and run.get("compute_cost_usd") is not None)
                               or (session and session.get("compute_cost_usd") is not None)) else "unknown",
        "data_provider_cost_usd": None,
        "experiment_total_cost_usd": None,
        "elapsed_worker_seconds": run.get("elapsed_seconds") if run else None,
        "elapsed_session_seconds": elapsed_session,
        "cash_receipts_usd": str(cash_receipts) if cash_linked else None,
        "cash_expenses_usd": str(cash_expenses) if cash_linked else None,
        "cash_basis": "operator-reported, unreconciled exact activity links" if cash_linked else "unknown",
        "cash_entry_count": cash_count,
        "fees_usd": None,
        "paper_outcome": paper_outcome,
        "information_outcome": information,
        "specialist_contributions": contributions,
        "full_net_economic_profit_usd": None,
        "allocation_follow_up": allocation_follow_up,
        "allocation_policy": "Measured failures or data defects may reduce attention; activity alone earns no increase",
        "source_ids": {"mission_id": mission["mission_id"], "trial_id": trial_id,
                       "run_id": run_id, "session_id": session_id},
    }


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value[:4000] if isinstance(value, str) else None


def _object(value: str) -> dict:
    if len(value) > 131072:
        raise ValueError("oversized record")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("invalid record")
    return parsed


def build_operations(
    path: str, *, additional_paths: tuple[str, ...] = (), now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    result: dict[str, Any] = {
        "as_of": now.isoformat(), "database_present": Path(path).is_file(),
        "runtime": {"state": "unknown", "health": "unknown", "last_heartbeat_at": None}, "sections": {},
        "resources": resource_status(),
        "economic_state": {"status": "not_recorded", "created_at": None},
        "economic_lanes": {"status": "not_recorded", "lanes": []},
    }
    for name in SECTIONS:
        result["sections"][name] = {"status": "not_recorded", "rows": [], "has_more": False}
    if not result["database_present"]:
        # A missing worker replica must not hide durable console-owned state.
        # The sidecar projector opens its sources read-only and records only
        # sections backed by rows it can actually read; worker runtime and
        # other worker-owned sections remain unknown/not-recorded here.
        _append_additional_console_records(result, path, additional_paths)
        result["openclaw_worker"] = openclaw_runtime_status(path)
        return result
    conn = None
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result["economic_lanes"] = _economic_lane_projection(conn, tables)
        if "economic_snapshots" in tables:
            try:
                row = conn.execute(
                    "SELECT created_at,snapshot_json FROM economic_snapshots ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    payload = _object(row["snapshot_json"])
                    keys = ("reserve_usd", "strategy_capital_usd", "research_budget_usd",
                            "infrastructure_budget_usd")
                    balances = {}
                    for key in keys:
                        amount = Decimal(str(payload[key]))
                        if not amount.is_finite() or amount < 0:
                            raise ValueError("invalid ledger balance")
                        balances[key] = str(amount)
                    result["economic_state"] = {
                        "status": "recorded", "created_at": _scalar(row["created_at"]),
                        "balances": balances,
                    }
            except (sqlite3.Error, ValueError, TypeError, KeyError, InvalidOperation):
                result["economic_state"]["status"] = "invalid"
        if "cognitive_sessions" in tables:
            try:
                session = conn.execute(
                    "SELECT session_id,status,objective,provider,model FROM cognitive_sessions "
                    "WHERE COALESCE(provider,'')!='openclaw' "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if session:
                    result["primary_cognition"] = {key: _scalar(session[key]) for key in dict(session)}
            except sqlite3.Error:
                result["primary_cognition"] = {"status": "unavailable"}
        if "agent_runtime" in tables:
            try:
                row = conn.execute(
                    "SELECT status_json FROM agent_runtime WHERE agent_id='noema'"
                ).fetchone()
                if row:
                    status = _object(row[0])
                    heartbeat = parse_aware_time(status["last_heartbeat_at"])
                    age = (now - heartbeat).total_seconds()
                    diagnostics = diagnose_runtime(
                        running=status.get("running") is True,
                        heartbeat_age_seconds=age,
                        identity_value=status.get("process_identity"),
                        database_path=str(Path(path).resolve()),
                        checkout_path=str(Path(__file__).resolve().parent.parent),
                        now=now,
                    )
                    state = diagnostics["state"].lower().replace(" ", "_")
                    if state == "live":
                        state = "running"
                    result["runtime"] = {
                        "state": state, "health": "unknown",
                        "last_heartbeat_at": heartbeat.isoformat(),
                        "liveness": diagnostics["state"],
                        "process_state": diagnostics["process_state"],
                        "process_identity": diagnostics["process_identity"],
                        "heartbeat_age_seconds": diagnostics["heartbeat_age_seconds"],
                    }
                    cycle = status.get("last_cycle")
                    if isinstance(cycle, dict):
                        result["runtime"]["health"] = _scalar(cycle.get("health")) or "unknown"
                        result["runtime"]["cycle"] = {
                            key: _scalar(cycle.get(key))
                            for key in ("cycle_id", "active_goal", "health", "started_at",
                                        "completed_at", "note", "ecosystem_state",
                                        "ecosystem_focus")
                        }
                        result["runtime"]["cycle"].update({
                            "duration_seconds": _scalar(cycle.get("duration_seconds")),
                            "cadence_seconds": _scalar(cycle.get("cadence_seconds")),
                            "stage_timings": cycle.get("stage_timings", {}),
                        })
                        if "agent_cycle_timings" in tables:
                            timing = conn.execute(
                                "SELECT duration_seconds,stage_timings_json FROM agent_cycle_timings WHERE cycle_id=?",
                                (cycle.get("cycle_id"),),
                            ).fetchone()
                            if timing:
                                result["runtime"]["cycle"]["duration_seconds"] = _scalar(timing[0])
                                result["runtime"]["cycle"]["stage_timings"] = _object(timing[1])
                        result["runtime"]["connections"] = {
                            key: {
                                "status": _scalar(cycle[key].get("status")),
                                "detail": _scalar(cycle[key].get("detail")),
                            }
                            for key in ("market_data", "kalshi", "cognition", "trench",
                                        "evm_wallet", "polymarket_us")
                            if isinstance(cycle.get(key), dict)
                        }
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                result["runtime"]["state"] = "invalid"
        for name, (table, columns, order) in SECTIONS.items():
            if table not in tables:
                continue
            section = result["sections"][name]
            try:
                if name == "wallet_transactions":
                    rows = conn.execute(
                        "SELECT id,created_at,event_type,amount_usd,payload_json FROM economic_events "
                        "WHERE event_type LIKE 'wallet_transaction_%' ORDER BY id DESC LIMIT 51"
                    ).fetchall()
                elif name == "experiments":
                    columns_present = {row[1] for row in conn.execute(
                        "PRAGMA table_info(research_trials)"
                    )}
                    selected = columns.split(",")
                    selected.extend(field for field in (
                        "params_json", "metadata_json", "strategy_id", "market_id", "venue",
                        "asset", "ticker", "status_updated_at",
                    ) if field in columns_present and field not in selected)
                    rows = conn.execute(
                        f"SELECT {','.join(selected)} FROM {table} ORDER BY {order} DESC LIMIT 51"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT {columns} FROM {table} ORDER BY {order} DESC LIMIT 51"
                    ).fetchall()
                section["has_more"] = len(rows) > 50
                for row in rows[:50]:
                    record = {key: _scalar(row[key]) for key in dict(row) if not key.endswith('_json')}
                    if name == "decisions":
                        try:
                            forecast, action = _object(row["forecast_json"]), _object(row["action_json"])
                            for key in ("model_version", "probability_yes", "lower_bound", "upper_bound",
                                        "asset", "underlying_asset", "strategy_id", "trial_id"):
                                record[key] = _scalar(forecast.get(key))
                            for key in ("decision", "reason"):
                                record[key] = _scalar(action.get(key))
                        except (ValueError, TypeError):
                            record["record_status"] = "invalid"
                    elif name in {"research_runs", "sessions"} and row["result_json"]:
                        try:
                            result_payload = _object(row["result_json"])
                            if name == "research_runs":
                                for key in ("observations", "observation_count"):
                                    value = result_payload.get(key)
                                    if (isinstance(value, int) and not isinstance(value, bool)
                                            and value >= 0):
                                        record["observations"] = value
                                        break
                                review = result_payload.get("critic_review")
                                if isinstance(review, dict):
                                    verdict = str(review.get("verdict", "")).strip().upper()
                                    if "result_accepted" not in review and verdict in {
                                        "PASS", "ACCEPT", "ACCEPTED", "FAIL", "REJECT", "REJECTED",
                                    }:
                                        review = {**review, "result_accepted": verdict in {
                                            "PASS", "ACCEPT", "ACCEPTED",
                                        }}
                                        result_payload = {**result_payload, "critic_review": review}
                            encoded_result = json.dumps(result_payload, sort_keys=True)
                            if name == "research_runs" and len(encoded_result) > 4000:
                                compact = {
                                    key: result_payload[key]
                                    for key in ("observations", "observation_count", "valid_markets",
                                                "critic_review", "verdict", "accepted")
                                    if key in result_payload
                                }
                                review = compact.get("critic_review")
                                if isinstance(review, dict):
                                    compact["critic_review"] = {
                                        key: (value[:500] if isinstance(value, str) else value)
                                        for key, value in review.items()
                                        if key in {"verdict", "result_accepted", "accepted", "reason", "status"}
                                    }
                                encoded_result = json.dumps(compact, sort_keys=True)
                            record["result"] = encoded_result[:4000]
                        except (ValueError, TypeError):
                            record["record_status"] = "invalid"
                    elif name == "experiments":
                        record = _project_experiment_record(row, conn, tables)
                    elif name == "missions":
                        for source, target in (("capability_grants_json", "capability_grants"),
                                               ("resource_grant_json", "resource_grant"),
                                               ("result_json", "result")):
                            if not row[source]:
                                continue
                            try:
                                if len(row[source]) > 131072:
                                    raise ValueError("oversized mission field")
                                decoded = json.loads(row[source])
                                if target == "capability_grants":
                                    if (not isinstance(decoded, list) or len(decoded) > 32
                                            or not all(isinstance(item, str) for item in decoded)):
                                        raise ValueError("invalid grants")
                                elif not isinstance(decoded, dict):
                                    raise ValueError("invalid mission object")
                                record[target] = decoded
                            except (ValueError, TypeError):
                                record["record_status"] = "invalid"
                        try:
                            record["measurement"] = _mission_measurement(conn, tables, row)
                        except (sqlite3.Error, ValueError, TypeError, KeyError):
                            record["measurement"] = {
                                "status": "unavailable", "full_net_economic_profit_usd": None,
                                "economic_lane": None, "model_api_cost_usd": None,
                                "compute_cost_usd": None, "data_provider_cost_usd": None,
                                "experiment_total_cost_usd": None, "cash_receipts_usd": None,
                                "cash_expenses_usd": None, "fees_usd": None,
                                "paper_outcome": None,
                            }
                    elif name in {"mission_events", "handoffs"}:
                        source = "payload_json" if name == "mission_events" else "result_json"
                        if row[source]:
                            try:
                                record["payload"] = _object(row[source])
                            except (ValueError, TypeError):
                                record["record_status"] = "invalid"
                    elif name == "wallet_transactions" and row["payload_json"]:
                        try:
                            record["payload"] = _object(row["payload_json"])
                        except (ValueError, TypeError):
                            record["record_status"] = "invalid"
                    section["rows"].append(record)
                section["status"] = "recorded" if rows else "empty"
            except sqlite3.Error:
                section["status"] = "unavailable"
        allocations = {"status": "not_recorded", "created_at": None,
                       "idle_fraction": None, "dominant_specialist": None, "rows": []}
        if "ecosystem_reviews" in tables:
            try:
                recent = conn.execute(
                    "SELECT id,created_at,dominant_specialist,idle_fraction,payload_json "
                    "FROM ecosystem_reviews ORDER BY id DESC LIMIT 100"
                ).fetchall()
                if recent:
                    plans = []
                    mission_review = None
                    mission_review_record = None
                    for review_row in recent:
                        review_payload = _object(review_row["payload_json"])
                        if not plans and isinstance(review_payload.get("allocations"), list):
                            plans.append(review_row)
                        candidate = review_payload.get("mission_review")
                        if mission_review is None and isinstance(candidate, dict):
                            mission_review = candidate
                            mission_review_record = review_row
                        if len(plans) >= 2 and mission_review is not None:
                            break
                    row = plans[0] if plans else None
                    if row is None:
                        raise ValueError("no allocation plan in review history")
                    plan = _object(row["payload_json"])
                    items = plan.get("allocations", [])
                    if not isinstance(items, list) or len(items) > 100:
                        raise ValueError("invalid specialist allocation record")
                    for item in items:
                        if not isinstance(item, dict):
                            raise TypeError("invalid specialist allocation")
                        share = item.get("attention_fraction")
                        if (not isinstance(share, (int, float)) or isinstance(share, bool)
                                or not math.isfinite(share) or not 0 <= share <= 1):
                            raise ValueError("invalid attention fraction")
                    previous = _object(plans[1]["payload_json"]) if len(plans) > 1 else {}
                    previous_items = previous.get("allocations", [])
                    previous_shares = {
                        item.get("specialist"): item.get("attention_fraction")
                        for item in previous_items if isinstance(item, dict)
                        and isinstance(item.get("attention_fraction"), (int, float))
                        and not isinstance(item.get("attention_fraction"), bool)
                    }
                    allocation_rows = []
                    for item in items:
                        current_share = float(item["attention_fraction"])
                        prior_share = previous_shares.get(item.get("specialist"))
                        delta = (current_share - float(prior_share)
                                 if isinstance(prior_share, (int, float)) and
                                 math.isfinite(prior_share) and 0 <= prior_share <= 1
                                 else None)
                        allocation_rows.append({key: _scalar(item.get(key)) for key in (
                            "specialist", "family", "state", "attention_fraction", "reason",
                        )} | {"attention_delta": delta})
                    allocations = {
                        "status": "recorded", "created_at": _scalar(row["created_at"]),
                        "previous_created_at": (_scalar(plans[1]["created_at"])
                                                 if len(plans) > 1 else None),
                        "dominant_specialist": _scalar(row["dominant_specialist"]),
                        "idle_fraction": _scalar(plan.get("idle_fraction")),
                        "mission_review": ({key: _scalar(mission_review.get(key)) for key in (
                            "mission_id", "specialist", "outcome", "basis",
                            "previous_attention_fraction", "current_attention_fraction",
                        )} | {"review_id": _scalar(
                            mission_review.get("review_id") or mission_review_record["id"]
                        ), "created_at": _scalar(mission_review_record["created_at"])}
                        if mission_review and mission_review_record else None),
                        "rows": allocation_rows,
                    }
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                allocations["status"] = "invalid"
        result["attention_allocations"] = allocations
    except sqlite3.Error:
        result["runtime"]["state"] = "unavailable"
        for section in result["sections"].values():
            section["status"] = "unavailable"
    finally:
        if conn is not None:
            conn.close()
    _append_additional_console_records(result, path, additional_paths)
    result["openclaw_worker"] = openclaw_runtime_status(path)
    return result


def _append_additional_console_records(
    result: dict[str, Any], primary_path: str, additional_paths: tuple[str, ...],
) -> None:
    """Project console-sidecar records into the existing operational sections."""
    seen_trials = {
        research_run_identity(row): index
        for index, row in enumerate(result["sections"]["research_runs"]["rows"])
    }
    seen_experiments = {
        str(row.get("trial_id")): index
        for index, row in enumerate(result["sections"]["experiments"]["rows"])
    }
    seen_wallet = {
        (row.get("created_at"), row.get("event_type"), row.get("amount_usd"),
         json.dumps(row.get("payload"), sort_keys=True, default=str))
        for row in result["sections"]["wallet_transactions"]["rows"]
    }
    seen_economic = {
        tuple(row.get(key) for key in (
            "created_at", "occurred_at", "provider", "event_type", "amount_usd",
            "reconciliation_state", "capital_class", "mission_id", "strategy_id", "lane",
        ))
        for row in result["sections"]["economic_events"]["rows"]
    }
    for source_index, path in enumerate(additional_paths):
        database = Path(path)
        if not database.is_file() or database.resolve() == Path(primary_path).resolve():
            continue
        conn: sqlite3.Connection | None = None
        decision_conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            decision_tables: set[str] = set()
            primary = Path(primary_path)
            if primary.is_file() and primary.resolve() != database.resolve():
                decision_conn = sqlite3.connect(
                    primary.resolve().as_uri() + "?mode=ro", uri=True, timeout=1,
                )
                decision_conn.row_factory = sqlite3.Row
                decision_conn.execute("PRAGMA query_only=ON")
                decision_tables = {row[0] for row in decision_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "autonomous_research_runs" in tables:
                columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(autonomous_research_runs)"
                )}
                required = {"trial_id", "specialist", "kind", "evidence_hash", "worker_version",
                            "status", "created_at", "result_json"}
                if required <= columns:
                    run_fields = (
                        "trial_id", "specialist", "kind", "evidence_hash", "worker_version",
                        "status", "created_at", "completed_at", "elapsed_seconds",
                        "compute_cost_usd", "result_json", "evidence_path", "mission_id",
                    )
                    selected_run_fields = [field for field in run_fields if field in columns]
                    for row in conn.execute(
                        f"SELECT {','.join(selected_run_fields)} FROM autonomous_research_runs "
                        "ORDER BY created_at DESC LIMIT 100"
                    ):
                        record = {
                            key: _scalar(row[key]) for key in selected_run_fields
                            if key != "result_json"
                        }
                        try:
                            payload = _object(row["result_json"])
                            for key in ("observations", "observation_count"):
                                count = payload.get(key)
                                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                                    record["observations"] = count
                                    break
                            review = payload.get("critic_review")
                            if isinstance(review, dict):
                                record["critic_review"] = review
                            record["result"] = json.dumps(payload, sort_keys=True)[:4000]
                        except (ValueError, TypeError):
                            record["record_status"] = "invalid"
                        identity = research_run_identity(record)
                        existing_index = seen_trials.get(identity)
                        if existing_index is None:
                            seen_trials[identity] = len(result["sections"]["research_runs"]["rows"])
                            result["sections"]["research_runs"]["rows"].append(record)
                        else:
                            existing_record = result["sections"]["research_runs"]["rows"][existing_index]
                            merged = merge_research_run_records(existing_record, record)
                            merged["id"] = existing_record.get("id", record.get("id"))
                            result["sections"]["research_runs"]["rows"][existing_index] = merged
            if "research_trials" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(research_trials)")}
                if "trial_id" in columns:
                    trial_fields = [field for field in (
                        "trial_id", "family", "hypothesis", "feature_set_version", "status",
                        "created_at", "parent_trial_id", "params_json", "metadata_json",
                        "strategy_id", "market_id", "venue", "asset", "ticker",
                        "status_updated_at",
                    ) if field in columns]
                    for row in conn.execute(
                        f"SELECT {','.join(trial_fields)} FROM research_trials "
                        + ("ORDER BY created_at DESC " if "created_at" in columns else "")
                        + "LIMIT 100"
                    ):
                        trial_id = str(row["trial_id"])
                        record = _project_experiment_record(
                            row, conn, tables, decision_conn=decision_conn,
                            decision_tables=decision_tables,
                        )
                        run_count_sources = (conn,) if decision_conn is None else (conn, decision_conn)
                        record.update(_combined_research_run_counts(run_count_sources, trial_id))
                        existing_index = seen_experiments.get(trial_id)
                        if existing_index is None:
                            seen_experiments[trial_id] = len(result["sections"]["experiments"]["rows"])
                            result["sections"]["experiments"]["rows"].append(record)
                        else:
                            existing_record = result["sections"]["experiments"]["rows"][existing_index]
                            if research_trial_update_is_newer(
                                record.get("status"), record.get("status_updated_at"),
                                existing_record.get("status"), existing_record.get("status_updated_at"),
                            ):
                                result["sections"]["experiments"]["rows"][existing_index] = {
                                    **existing_record, **record,
                                }
                            else:
                                for key, value in record.items():
                                    if ((key in {"run_count", "evidence_count"}
                                         and value is not None)
                                            or (existing_record.get(key) is None and value is not None)):
                                        existing_record[key] = value
            if "economic_events" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(economic_events)")}
                wallet_columns = {"id", "created_at", "event_type", "amount_usd", "payload_json"}
                if wallet_columns <= columns:
                    for row in conn.execute(
                        "SELECT id,created_at,event_type,amount_usd,payload_json FROM economic_events "
                        "WHERE event_type LIKE 'wallet_transaction_%' ORDER BY created_at DESC LIMIT 100"
                    ):
                        try:
                            payload = _object(row["payload_json"])
                        except (ValueError, TypeError):
                            payload = {}
                        identity = (row["created_at"], row["event_type"], row["amount_usd"],
                                    json.dumps(payload, sort_keys=True, default=str))
                        if identity in seen_wallet:
                            continue
                        seen_wallet.add(identity)
                        result["sections"]["wallet_transactions"]["rows"].append({
                            "id": f"console-state-{source_index}-{row['id']}",
                            "created_at": _scalar(row["created_at"]),
                            "event_type": _scalar(row["event_type"]),
                            "amount_usd": _scalar(row["amount_usd"]), "payload": payload,
                        })
                event_fields = (
                    "id", "created_at", "occurred_at", "provider", "event_type", "amount_usd",
                    "reconciliation_state", "capital_class", "mission_id", "strategy_id", "lane",
                )
                available = [field for field in event_fields if field in columns]
                required = {"id", "created_at", "event_type"}
                if required <= set(available):
                    selected = ",".join(available)
                    for row in conn.execute(
                        f"SELECT {selected} FROM economic_events ORDER BY created_at DESC LIMIT 100"
                    ):
                        identity = tuple(row[field] if field in available else None
                                         for field in event_fields[1:])
                        if identity in seen_economic:
                            continue
                        seen_economic.add(identity)
                        record = {field: _scalar(row[field]) for field in available if field != "id"}
                        record["id"] = f"console-state-{source_index}-{row['id']}"
                        result["sections"]["economic_events"]["rows"].append(record)
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
            if decision_conn is not None:
                decision_conn.close()
    for name in ("research_runs", "experiments", "wallet_transactions", "economic_events"):
        section = result["sections"][name]
        section["rows"].sort(
            key=lambda row: str(row.get("created_at") or ""), reverse=True,
        )
        section["has_more"] = section["has_more"] or len(section["rows"]) > 50
        section["rows"] = section["rows"][:50]
        if section["rows"]:
            section["status"] = "recorded"
