"""One research-only Responses request contract shared by supported providers."""

from __future__ import annotations

import json
from typing import Any

from .agent_identity import AgentIdentity
from .opportunity_radar import RadarRow

PACKET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "attention_reason": {"type": "string"},
        "counterarguments": {
            "type": "array",
            "items": {"type": "string"},
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
        },
        "requested_research": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommended_mode": {
            "type": "string",
            "enum": ["ignore", "collect_more", "investigate"],
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "thesis",
        "confidence",
        "attention_reason",
        "counterarguments",
        "unknowns",
        "requested_research",
        "recommended_mode",
        "evidence_ids",
    ],
    "additionalProperties": False,
}



def build_cognition_request(
    row: RadarRow, *, evidence_context: list[dict[str, object]],
    model: str, reasoning_effort: str, max_output_tokens: int,
) -> dict[str, Any]:
    observed = {
        "market_id": row.market_id,
        "title": row.title,
        "model_probability_yes": row.probability_yes,
        "market_probability": row.market_probability,
        "yes_ask": row.yes_ask,
        "raw_edge": row.raw_edge,
        "estimated_cost": row.estimated_cost,
        "uncertainty_penalty": row.uncertainty_penalty,
        "robust_edge": row.robust_edge,
        "spread": row.spread,
        "liquidity_usd": row.liquidity_usd,
        "freshness_seconds": row.freshness_seconds,
        "uncertainty_width": row.uncertainty_width,
        "current_decision": row.decision,
        "current_reason": row.reason,
        "evidence_ids": list(row.evidence_ids),
    }
    if not evidence_context or {str(e.get("evidence_id")) for e in evidence_context} != set(
        row.evidence_ids
    ):
        raise ValueError("verified evidence context required")
    instructions = AgentIdentity().specialist_instructions("kalshi-history") + (
        "\nCURRENT TASK AND AUTHORITY: Research triage of the supplied market only. "
        "Analyze only the supplied facts. State missing information in unknowns. "
        "This call has no execution, tool, worker, spending, or promotion authority. "
        "Never suggest a stake or trading action. Recommend only ignore, collect_more, "
        "or investigate. Explain whether further research is economically justified; "
        "when cost or value is unknown, name that uncertainty. Evidence IDs must "
        "come from the input. Return only the structured research packet."
    )
    inputs = {"market": observed, "verified_evidence": evidence_context}
    request = {
        "model": model,
        "instructions": instructions,
        "input": json.dumps(inputs, sort_keys=True),
        "max_output_tokens": max_output_tokens,
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "noema_cognition_packet",
                "schema": PACKET_SCHEMA,
                "strict": True,
            }
        },
    }
    # GPT-4.1/4o are non-reasoning model families. Their API surface does not
    # accept a reasoning-effort override; omit it rather than sending a guess.
    if not model.startswith(("gpt-4.1", "gpt-4o")):
        request["reasoning"] = {"effort": reasoning_effort}
    return request
