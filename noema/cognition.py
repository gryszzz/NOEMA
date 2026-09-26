from __future__ import annotations

import json
import sqlite3

import httpx

from .cognition_models import CognitionResult
from .cognition_policy import CognitionPolicy, assess_cognition
from .cognition_store import CognitionStore
from .foundry_client import FoundryCognitionClient
from .foundry_config import FoundryConfig
from .llm_evidence import context_for_row
from .opportunity_radar import RadarRow
from .provenance import EvidenceStore
from .research_queue import ResearchQueueStore


async def maybe_run_cognition(
    rows: list[RadarRow],
    *,
    db_path: str,
    config: FoundryConfig | None = None,
    policy: CognitionPolicy | None = None,
) -> CognitionResult:
    config = config or FoundryConfig.from_env()
    policy = policy or CognitionPolicy.from_env()

    if not config.enabled:
        return CognitionResult("disabled", detail="cognition disabled")
    if not config.ready:
        return CognitionResult("unconfigured", detail="Foundry not configured")

    store = CognitionStore(db_path)
    eligible: list[RadarRow] = []
    gate_reasons: list[str] = []
    for row in rows:
        gate = assess_cognition(row, store, policy)
        if gate.eligible:
            eligible.append(row)
        elif not gate_reasons:
            gate_reasons.extend(gate.reasons)

    if not eligible:
        return CognitionResult(
            "idle",
            detail="; ".join(gate_reasons) or "no radar rows",
        )

    eligible.sort(
        key=lambda row: row.attention_score or 0.0,
        reverse=True,
    )
    target = eligible[0]
    try:
        context = context_for_row(target, EvidenceStore(db_path))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return CognitionResult("idle", detail="candidate evidence unavailable or invalid")
    try:
        client = FoundryCognitionClient(config)
    except (RuntimeError, ValueError) as exc:
        return CognitionResult(
            "degraded",
            detail=f"{type(exc).__name__}: model setup failed",
        )

    try:
        body = client.request_body(target, evidence_context=context)
        instructions = str(body["instructions"])
        inputs = str(body["input"])
        max_cost = policy.estimated_max_call_usd(
            input_bytes=len((instructions + inputs).encode("utf-8")),
            max_output_tokens=config.max_output_tokens,
        )
        if not store.reserve_estimated_cost(
            max_cost, daily_limit_usd=policy.max_estimated_usd_per_day,
        ):
            await client.close()
            return CognitionResult("idle", detail="daily estimated model budget exhausted")
    except (ValueError, KeyError, TypeError):
        await client.close()
        return CognitionResult("idle", detail="model price or daily budget unavailable")
    except sqlite3.Error:
        await client.close()
        return CognitionResult("degraded", detail="model budget store unavailable")

    try:
        result = await client.reason_about_market(target, evidence_context=context)
    except (
        httpx.HTTPError,
        RuntimeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        return CognitionResult(
            "degraded",
            detail=f"{type(exc).__name__}: model request failed",
        )
    finally:
        await client.close()

    if result.packet is None:
        return CognitionResult("degraded", detail="Foundry returned no packet")

    store.append(
        deployment=str(config.deployment),
        packet=result.packet,
        response_id=result.detail,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
    )

    queue = ResearchQueueStore(db_path)
    for request in result.packet.requested_research:
        queue.enqueue(
            market_id=result.packet.market_id,
            request=request,
            priority=result.packet.confidence,
        )
    return result
