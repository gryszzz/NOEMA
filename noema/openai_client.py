"""Budget-gated OpenAI research cognition, without execution or hosted tools."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from .cognition_models import CognitionPacket, CognitionResult
from .cognition_request import PACKET_SCHEMA, build_cognition_request
from .openai_config import OPENAI_RESPONSES_URL, OpenAIConfig
from .openai_tracing import openai_trace
from .opportunity_radar import RadarRow


def _completed_text(payload: dict[str, Any]) -> str:
    if payload.get("status") != "completed" or payload.get("error"):
        raise ValueError("OpenAI response did not complete")
    parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            raise TypeError("invalid OpenAI response item")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise ValueError("unexpected OpenAI response item")
        for content in item.get("content", []):
            if (not isinstance(content, dict) or content.get("type") != "output_text"
                    or not isinstance(content.get("text"), str)):
                raise ValueError("OpenAI response refused or contained unexpected content")
            parts.append(content["text"])
    if not parts:
        raise ValueError("OpenAI response contained no research packet")
    return "".join(parts)


def _parse_packet(raw: object, row: RadarRow) -> CognitionPacket:
    if not isinstance(raw, dict) or set(raw) != set(PACKET_SCHEMA["required"]):
        raise ValueError("invalid cognition packet fields")
    for field in ("thesis", "attention_reason", "recommended_mode"):
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ValueError("invalid cognition packet text")
    for field in ("counterarguments", "unknowns", "requested_research", "evidence_ids"):
        if (not isinstance(raw[field], list)
                or any(not isinstance(value, str) for value in raw[field])):
            raise ValueError("invalid cognition packet list")
    confidence = raw["confidence"]
    if (type(confidence) not in (int, float) or not math.isfinite(confidence)
            or not 0 <= confidence <= 1):
        raise ValueError("invalid cognition packet confidence")
    if raw["recommended_mode"] not in {"ignore", "collect_more", "investigate"}:
        raise ValueError("cognition packet exceeded research authority")
    if not set(raw["evidence_ids"]).issubset(row.evidence_ids):
        raise ValueError("cognition packet cited unavailable evidence")
    return CognitionPacket(
        market_id=row.market_id, thesis=raw["thesis"], confidence=float(confidence),
        attention_reason=raw["attention_reason"],
        counterarguments=tuple(raw["counterarguments"]), unknowns=tuple(raw["unknowns"]),
        requested_research=tuple(raw["requested_research"]),
        recommended_mode=raw["recommended_mode"], evidence_ids=tuple(raw["evidence_ids"]),
    )


class OpenAICognitionClient:
    def __init__(
        self, config: OpenAIConfig | None = None, client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or OpenAIConfig.from_env()
        self.config.validate()
        if not self.config.ready:
            raise RuntimeError("OpenAI cognition is not explicitly enabled and configured")
        self._headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "NOEMA/0.1",
        }
        if self.config.project_id:
            self._headers["OpenAI-Project"] = self.config.project_id
        self.last_trace_id: str | None = None
        self.last_trace_status: str = "not_started"
        # No retries: an uncertain failed request keeps its existing reservation.
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.request_timeout_seconds), follow_redirects=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def request_body(
        self, row: RadarRow, *, evidence_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        return build_cognition_request(
            row, evidence_context=evidence_context, model=str(self.config.model),
            reasoning_effort=self.config.reasoning_effort,
            max_output_tokens=self.config.max_output_tokens,
        )

    async def reason_about_market(
        self, row: RadarRow, *, evidence_context: list[dict[str, object]],
        trace_metadata: dict[str, str] | None = None,
    ) -> CognitionResult:
        payload = await self.structured_research(
            self.request_body(row, evidence_context=evidence_context),
            trace_metadata=trace_metadata,
        )
        packet = _parse_packet(json.loads(_completed_text(payload)), row)
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            raise TypeError("OpenAI usage unavailable")
        counts = tuple(usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens"))
        if (any(type(value) is not int or value <= 0 for value in counts)
                or counts[2] < counts[0] + counts[1]):
            raise ValueError("OpenAI usage is invalid")
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ValueError("OpenAI response identifier unavailable")
        return CognitionResult(
            status="completed", packet=packet, detail=response_id,
            input_tokens=counts[0], output_tokens=counts[1], total_tokens=counts[2],
            trace_id=self.last_trace_id, trace_status=self.last_trace_status,
        )

    async def structured_research(
        self, body: dict[str, Any], *, trace_metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            with openai_trace(
                trace_metadata or {}, enabled=self.config.tracing_enabled,
            ) as run:
                response = await self.client.post(
                    OPENAI_RESPONSES_URL,
                    json=body,
                    headers=self._headers,
                )
        finally:
            self.last_trace_id = run.trace_id
            self.last_trace_status = run.status
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("invalid OpenAI response")
        _completed_text(payload)
        return payload
