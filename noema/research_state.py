"""Recency rules shared by durable research-state merges and projections."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_TRIAL_STATUS_RANK = {
    "registered": 0,
    "running": 1,
    "rejected": 2,
    "promoted": 2,
    "retired": 2,
    "completed": 2,
    "failed": 2,
    "cancelled": 2,
    "expired": 2,
}
_TERMINAL_RUN_STATUSES = {
    "completed", "failed", "rejected", "interrupted", "expired", "cancelled",
}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def research_trial_update_is_newer(
    incoming_status: Any, incoming_at: Any, current_status: Any, current_at: Any,
) -> bool:
    """Compare mutable trial state without replacing immutable trial identity."""
    incoming = str(incoming_status or "").casefold()
    current = str(current_status or "").casefold()
    incoming_rank = _TRIAL_STATUS_RANK.get(incoming, -1)
    current_rank = _TRIAL_STATUS_RANK.get(current, -1)
    if incoming_rank != current_rank:
        return incoming_rank > current_rank
    incoming_time, current_time = _timestamp(incoming_at), _timestamp(current_at)
    return incoming_time is not None and (current_time is None or incoming_time > current_time)


def research_run_identity(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Use persisted experiment/evidence/version identifiers; timestamp fallback is legacy-only."""
    trial_id = str(record.get("trial_id") or "")
    evidence_hash = str(record.get("evidence_hash") or "")
    worker_version = str(record.get("worker_version") or "")
    fallback = "" if evidence_hash or worker_version else str(record.get("created_at") or "")
    return trial_id, evidence_hash, worker_version, fallback


def research_run_is_preferred(incoming: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Prefer terminal, newer, then more complete representations; ties favor the later source."""
    incoming_status = str(incoming.get("status") or "").casefold()
    current_status = str(current.get("status") or "").casefold()
    incoming_terminal = incoming_status in _TERMINAL_RUN_STATUSES
    current_terminal = current_status in _TERMINAL_RUN_STATUSES
    if incoming_terminal != current_terminal:
        return incoming_terminal

    incoming_at = _timestamp(incoming.get("completed_at") or incoming.get("updated_at"))
    current_at = _timestamp(current.get("completed_at") or current.get("updated_at"))
    if incoming_at is not None and current_at is not None and incoming_at != current_at:
        return incoming_at > current_at
    if incoming_at is None and current_at is not None:
        return False
    if incoming_at is not None and current_at is None:
        return True

    def completeness(record: Mapping[str, Any]) -> tuple[int, ...]:
        result = record.get("result") or record.get("result_json")
        has_result = bool(result and str(result).strip() not in {"", "{}", "[]"})
        return (
            int(record.get("status") == "completed"),
            int(bool(record.get("completed_at"))),
            int(has_result),
            int(record.get("observations") is not None),
            int(record.get("critic_review") is not None),
            int(record.get("elapsed_seconds") is not None),
            int(record.get("compute_cost_usd") is not None),
            int(record.get("evidence_path") is not None),
        )

    # Choosing incoming on equal completeness makes the later-read sidecar the
    # tie-breaker while keeping all preference criteria explicit and stable.
    return completeness(incoming) >= completeness(current)


def merge_research_run_records(
    current: Mapping[str, Any], incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Choose the authoritative version and retain non-conflicting known fields."""
    incoming_wins = research_run_is_preferred(incoming, current)
    preferred = dict(incoming if incoming_wins else current)
    fallback = current if incoming_wins else incoming
    authoritative_fields = {"status", "completed_at", "updated_at", "created_at"}
    for key, value in fallback.items():
        if key not in authoritative_fields and preferred.get(key) in (None, "") and value not in (None, ""):
            preferred[key] = value
    return preferred
