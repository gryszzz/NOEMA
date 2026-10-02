"""Owner-triggered, budgeted cognition diagnostics with no execution surface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .bill_tracker import BillTracker
from .cognition_config import cognition_config_from_env
from .cognition_policy import CognitionPolicy
from .cognition_store import CognitionStore
from .openai_client import OpenAICognitionClient
from .openai_config import OpenAIConfig
from .provenance import canonical_payload_hash

MAX_OUTPUT_TOKENS = 1000
TRIGGER_ENV = "NOEMA_COGNITION_DIAGNOSTIC_TRIGGER_ID"
_TRIGGER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "data_quality_blockers": {"type": "array", "items": {"type": "string"}},
        "next_observation": {"type": "string"},
        "supplied_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "action": {"type": "string", "enum": ["observe_only"]},
    },
    "required": ["summary", "findings", "data_quality_blockers", "next_observation",
                 "supplied_evidence_ids", "action"],
    "additionalProperties": False,
}


def _ensure_diagnostic_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cognition_diagnostics (
            trigger_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            subsystem TEXT NOT NULL,
            status TEXT NOT NULL,
            execution_class TEXT NOT NULL CHECK (execution_class = 'non-executable'),
            context_summary_json TEXT NOT NULL,
            context_sha256 TEXT NOT NULL,
            result_json TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost_usd TEXT,
            model_budget_remaining_usd TEXT,
            failure_class TEXT
        )"""
    )
    conn.commit()


def _persisted_context(db_path: str, *, limit: int = 8) -> dict[str, Any]:
    """Build a bounded diagnostic input from persisted observations, never live guesses."""
    if not Path(db_path).exists():
        raise ValueError("persisted market/research database is unavailable")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "forecast_ledger" not in tables:
            raise ValueError("persisted market evidence is unavailable")
        now = datetime.now(UTC)
        forecasts: list[dict[str, Any]] = []
        evidence_ids: set[str] = set()
        for row in conn.execute(
            "SELECT venue, market_id, snapshot_json, forecast_json, opportunity_json, "
            "action_json, created_at FROM forecast_ledger ORDER BY id DESC LIMIT ?", (limit,),
        ):
            snapshot = json.loads(row["snapshot_json"])
            forecast = json.loads(row["forecast_json"])
            opportunity = json.loads(row["opportunity_json"])
            action = json.loads(row["action_json"])
            observed = snapshot.get("captured_at") or row["created_at"]
            stamp = datetime.fromisoformat(str(observed))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            age = max(0.0, (now - stamp.astimezone(UTC)).total_seconds())
            ids = [str(value) for value in forecast.get("evidence_ids", [])
                   if isinstance(value, str)]
            evidence_ids.update(ids)
            forecasts.append({
                "venue": str(row["venue"]), "market_id": str(row["market_id"]),
                "title": str(snapshot.get("title") or row["market_id"])[:240],
                "captured_at": stamp.astimezone(UTC).isoformat(),
                "age_seconds_at_context_build": round(age, 2),
                "yes_bid": snapshot.get("yes_bid"), "yes_ask": snapshot.get("yes_ask"),
                "liquidity_usd": snapshot.get("liquidity_usd"),
                "probability_yes": forecast.get("probability_yes"),
                "model_version": forecast.get("model_version"),
                "raw_edge": opportunity.get("raw_edge"),
                "robust_edge": opportunity.get("robust_edge"),
                "decision": action.get("decision"), "reason": action.get("reason"),
                "evidence_ids": ids,
            })

        evidence: list[dict[str, Any]] = []
        if "evidence_records" in tables and evidence_ids:
            for evidence_id in sorted(evidence_ids)[:8]:
                row = conn.execute(
                    "SELECT evidence_id, source_type, observed_at, retrieved_at, payload_hash, payload_json "
                    "FROM evidence_records WHERE evidence_id=?", (evidence_id,),
                ).fetchone()
                if row is not None:
                    payload = json.loads(row["payload_json"])
                    if canonical_payload_hash(payload) != row["payload_hash"]:
                        raise ValueError("persisted research evidence failed integrity verification")
                    evidence.append({
                        "evidence_id": row["evidence_id"],
                        "source_type": row["source_type"],
                        "observed_at": row["observed_at"],
                        "retrieved_at": row["retrieved_at"],
                        "payload_hash": row["payload_hash"],
                        "integrity_state": "verified",
                    })
        if not forecasts:
            raise ValueError("no persisted market snapshots are available")
        return {
            "as_of": now.isoformat(),
            "forecast_rows": forecasts,
            "verified_evidence_records": evidence,
            "evidence_ids_supplied": sorted({r for f in forecasts for r in f["evidence_ids"]}),
            "fresh_rows_under_120s": sum(
                f["age_seconds_at_context_build"] <= 120 for f in forecasts),
            "stale_rows_over_120s": sum(
                f["age_seconds_at_context_build"] > 120 for f in forecasts),
            "execution_class": "diagnostic/non-executable",
        }
    finally:
        conn.close()


def _request_body(config: OpenAIConfig, context: dict[str, Any]) -> dict[str, Any]:
    instructions = (
        "You are reviewing persisted NOEMA evidence for a bounded owner-requested "
        "cognition diagnostic. This is diagnostic and non-executable. Use only the "
        "supplied persisted observations; explicitly identify stale or missing data. "
        "Do not make a trade recommendation, choose a position, or claim profitability. "
        "Do not request or imply orders, trades, transfers, signing, credentials, or "
        "authority changes. Return action=observe_only and cite only supplied evidence IDs."
    )
    body: dict[str, Any] = {
        "model": config.model,
        "instructions": instructions,
        "input": json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False),
        "max_output_tokens": min(config.max_output_tokens, MAX_OUTPUT_TOKENS),
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "noema_cognition_diagnostic",
                             "schema": _RESULT_SCHEMA, "strict": True}},
    }
    if config.supports_reasoning_effort:
        body["reasoning"] = {"effort": config.reasoning_effort}
    return body


def _parse_result(payload: dict[str, Any], supplied_ids: set[str]) -> dict[str, Any]:
    from .openai_client import _completed_text

    raw = json.loads(_completed_text(payload))
    if not isinstance(raw, dict) or set(raw) != set(_RESULT_SCHEMA["required"]):
        raise ValueError("diagnostic result did not match the required schema")
    for name in ("summary", "next_observation"):
        if not isinstance(raw[name], str):
            raise TypeError("diagnostic result contains an invalid text field")
    for name in ("findings", "data_quality_blockers", "supplied_evidence_ids"):
        if not isinstance(raw[name], list) or any(not isinstance(v, str) for v in raw[name]):
            raise ValueError("diagnostic result contains an invalid list")
    if raw["action"] != "observe_only" or not set(raw["supplied_evidence_ids"]).issubset(supplied_ids):
        raise ValueError("diagnostic result exceeded its non-executable evidence contract")
    return raw


async def run_owner_diagnostic(db_path: str) -> dict[str, Any] | None:
    """Consume one explicit worker environment trigger and make at most one request."""
    trigger_id = os.getenv(TRIGGER_ENV, "").strip()
    if not trigger_id:
        return None
    if not _TRIGGER_PATTERN.fullmatch(trigger_id):
        return {"status": "blocked", "reason": "invalid diagnostic trigger identifier"}

    store = CognitionStore(db_path)
    decision_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    context: dict[str, Any] = {}
    context_json = "{}"
    try:
        context = _persisted_context(db_path)
        context_json = json.dumps(
            context, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        context = {"execution_class": "diagnostic/non-executable", "context_status": "unavailable"}
        context_json = json.dumps(context, sort_keys=True)
        _claim(store, trigger_id, decision_id, created_at, context_json)
        result = _finish_blocked(
            store, trigger_id, "persisted evidence unavailable", type(exc).__name__,
        )
        store.conn.close()
        return result

    if not _claim(store, trigger_id, decision_id, created_at, context_json):
        row = store.conn.execute(
            "SELECT status FROM cognition_diagnostics WHERE trigger_id=?", (trigger_id,),
        ).fetchone()
        store.conn.close()
        return {"status": "already_consumed", "previous_status": row[0] if row else "unknown"}

    try:
        raw_config = cognition_config_from_env()
        if not isinstance(raw_config, OpenAIConfig):
            raise TypeError("configured cognition provider is not OpenAI")
        raw_config.validate()
        if raw_config.model != "gpt-5.6-luna":
            raise ValueError("configured model is not gpt-5.6-luna")
        if not raw_config.ready:
            raise ValueError("OpenAI cognition is not enabled and configured")
        config = replace(raw_config, max_output_tokens=min(raw_config.max_output_tokens,
                                                            MAX_OUTPUT_TOKENS))
        policy = CognitionPolicy.from_env(provider="openai", model=config.model)
        policy = replace(policy, max_calls_per_hour=min(policy.max_calls_per_hour, 1),
                         max_estimated_usd_per_day=min(policy.max_estimated_usd_per_day, 0.01))
        if policy.max_calls_per_hour < 1 or policy.max_estimated_usd_per_day <= 0:
            raise ValueError("diagnostic cognition budget is unavailable")
        bill = BillTracker(db_path)
        try:
            overview = bill.overview()
            if overview.get("status") not in {
                "within_owner_limit", "within_budget", "covered_by_reported_receipts",
            }:
                raise ValueError("durable hosted bill budget is not ready")
            monthly = Decimal(str(overview.get("model_budget_usd") or "0"))
        finally:
            bill.conn.close()
        if monthly <= 0:
            raise ValueError("monthly model budget is zero")

        body = _request_body(config, context)
        estimate = policy.estimated_max_call_usd(
            input_bytes=len(json.dumps(
                body, ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")) + 256,
            max_output_tokens=config.max_output_tokens,
        )
        if estimate > policy.max_estimated_usd_per_day:
            raise ValueError("conservative call estimate exceeds the daily cognition cap")
        remaining_before = store.monthly_model_budget_remaining(monthly)
        if Decimal(str(estimate)) > remaining_before:
            raise ValueError("monthly model budget remaining is below the call reservation")
        reserved = store.reserve_estimated_cost(
            estimate, daily_limit_usd=policy.max_estimated_usd_per_day,
            hourly_call_limit=1, monthly_limit_usd=float(monthly),
            estimated_tokens=(len(body["input"].encode("utf-8")) + config.max_output_tokens),
            hourly_token_limit=policy.max_tokens_per_hour,
            provider="openai", model=config.model, activity_id=decision_id,
        )
        if not reserved:
            raise ValueError("daily, hourly, token, or monthly model budget exhausted")

        client = OpenAICognitionClient(
            config, usage_db_path=db_path, subsystem="owner_diagnostic", decision_id=decision_id,
        )
        try:
            payload = await client.structured_research(body, trace_metadata={
                "decision_id": decision_id, "specialist": "owner-cognition-diagnostic",
                "mission_id": "owner-triggered-diagnostic", "provider": "openai",
                "financial_mode": "diagnostic-non-executable",
                "authority_state": "no-execution-authority",
            })
        finally:
            await client.close()

        usage = payload.get("usage")
        if not isinstance(usage, dict):
            raise TypeError("OpenAI usage metadata unavailable")
        input_tokens, output_tokens, total_tokens = (
            usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"))
        if any(type(v) is not int or v < 0 for v in (input_tokens, output_tokens, total_tokens)):
            raise ValueError("OpenAI usage metadata invalid")
        if total_tokens < input_tokens + output_tokens:
            raise ValueError("OpenAI usage totals are inconsistent")
        result = _parse_result(payload, set(context["evidence_ids_supplied"]))
        input_rate = Decimal(str(policy.input_usd_per_million))
        output_rate = Decimal(str(policy.output_usd_per_million))
        actual_cost = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / 1_000_000
        remaining = store.monthly_model_budget_remaining(monthly)
        response_id = payload.get("id")
        persisted = _complete(store, trigger_id, result, input_tokens, output_tokens,
                              actual_cost, remaining)
        return {"status": "completed", "persisted": persisted, "decision_id": decision_id,
                "response_id_present": bool(response_id), "input_tokens": input_tokens,
                "output_tokens": output_tokens, "estimated_cost_usd": str(actual_cost),
                "model_budget_remaining_usd": str(remaining), "evidence_count": len(context["evidence_ids_supplied"]),
                "context_sha256": hashlib.sha256(context_json.encode()).hexdigest(),
                "context_summary": context, "result": result}
    except Exception as exc:  # noqa: BLE001 - keep worker alive; consumed trigger never retries.
        failure_class = type(exc).__name__
        _finish_blocked(store, trigger_id, "diagnostic did not complete", failure_class)
        return {"status": "failed", "reason": "diagnostic did not complete",
                "failure_class": failure_class, "decision_id": decision_id}
    finally:
        store.conn.close()


def _claim(store: CognitionStore, trigger_id: str, decision_id: str,
           created_at: str, context_json: str) -> bool:
    _ensure_diagnostic_table(store.conn)
    try:
        store.conn.execute(
            "INSERT INTO cognition_diagnostics (trigger_id, decision_id, created_at, subsystem, "
            "status, execution_class, context_summary_json, context_sha256) "
            "VALUES (?, ?, ?, 'owner_diagnostic', 'started', 'non-executable', ?, ?)",
            (trigger_id, decision_id, created_at, context_json,
             hashlib.sha256(context_json.encode()).hexdigest()),
        )
        store.conn.commit()
        return True
    except sqlite3.IntegrityError:
        store.conn.rollback()
        return False


def _finish_blocked(store: CognitionStore, trigger_id: str, reason: str,
                    failure_class: str | None = None) -> dict[str, Any]:
    store.conn.execute(
        "UPDATE cognition_diagnostics SET status='blocked', completed_at=?, result_json=?, "
        "failure_class=? WHERE trigger_id=? AND status='started'",
        (datetime.now(UTC).isoformat(), json.dumps({"reason": reason}), failure_class, trigger_id),
    )
    store.conn.commit()
    return {"status": "blocked", "reason": reason}


def _complete(store: CognitionStore, trigger_id: str, result: dict[str, Any],
              input_tokens: int, output_tokens: int, actual_cost: Decimal,
              remaining: Decimal) -> bool:
    cursor = store.conn.execute(
        "UPDATE cognition_diagnostics SET status='completed', completed_at=?, result_json=?, "
        "input_tokens=?, output_tokens=?, estimated_cost_usd=?, "
        "model_budget_remaining_usd=? WHERE trigger_id=? AND status='started'",
        (datetime.now(UTC).isoformat(), json.dumps(result, sort_keys=True), input_tokens,
         output_tokens, str(actual_cost), str(remaining), trigger_id),
    )
    store.conn.commit()
    return cursor.rowcount == 1
