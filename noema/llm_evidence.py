"""Small, verified, data-only evidence view for the LLM research layer."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from .opportunity_radar import RadarRow
from .provenance import EvidenceStore

MAX_EVIDENCE = 4


def context_for_row(row: RadarRow, store: EvidenceStore) -> list[dict[str, object]]:
    """Reject missing/tampered evidence; never send unbounded raw payloads."""
    if not row.evidence_ids or len(row.evidence_ids) > MAX_EVIDENCE:
        raise ValueError("missing or excessive evidence")
    now = datetime.now(UTC)
    captured = datetime.fromisoformat(row.captured_at)
    if captured.tzinfo is None:
        raise ValueError("market snapshot has no timezone")
    result: list[dict[str, object]] = []
    for evidence_id in row.evidence_ids:
        if not re.fullmatch(r"[A-Za-z0-9:._-]{1,160}", evidence_id):
            raise ValueError("invalid evidence identifier")
        record = store.get(evidence_id)
        if record is None or not store.verify_integrity(evidence_id):
            raise ValueError("evidence missing or integrity failed")
        if (
            record.observed_at.tzinfo is None or record.observed_at > now
            or record.observed_at > captured
        ):
            raise ValueError("evidence observed in the future")
        if record.source_type != "historical_outcomes":
            raise ValueError("unsupported evidence type")
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            raise TypeError("invalid evidence payload")
        series, events, yes, markets = (
            payload.get("series"), payload.get("events"), payload.get("yes"),
            payload.get("markets_per_event"),
        )
        if (
            not isinstance(series, str) or not series.isalnum() or len(series) > 32
            or type(events) is not int or events < 30
            or type(yes) is not int or type(markets) is not int
            or markets not in {1, 2} or not 0 <= yes <= events * markets
        ):
            raise ValueError("invalid historical outcome summary")
        result.append({
            "evidence_id": evidence_id,
            "source_type": record.source_type,
            "observed_at": record.observed_at.isoformat(),
            "payload_hash": record.payload_hash,
            "series": series,
            "events": events,
            "yes_outcomes": yes,
            "markets_per_event": markets,
        })
    return result
