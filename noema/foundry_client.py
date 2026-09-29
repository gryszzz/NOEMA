from __future__ import annotations

import json
from typing import Any

import httpx

from .cognition_models import CognitionPacket, CognitionResult
from .cognition_request import build_cognition_request
from .foundry_config import FoundryConfig, responses_url
from .opportunity_radar import RadarRow


def _output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    return text
    raise RuntimeError("Foundry response contained no output_text")


def _usage(payload: dict[str, Any]) -> tuple[int, int, int]:
    usage = payload.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(
        usage.get("total_tokens")
        or input_tokens + output_tokens
    )
    return input_tokens, output_tokens, total_tokens


class FoundryCognitionClient:
    def __init__(
        self,
        config: FoundryConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or FoundryConfig.from_env()
        self.config.validate()
        if not self.config.ready:
            raise RuntimeError("Foundry cognition is not fully configured")
        self._headers = {
            "api-key": str(self.config.api_key),
            "Content-Type": "application/json",
            "User-Agent": "NOEMA/0.1",
        }
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.request_timeout_seconds),
        )

    async def close(self) -> None:
        await self.client.aclose()

    def request_body(
        self, row: RadarRow, *, evidence_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        return build_cognition_request(
            row, evidence_context=evidence_context, model=str(self.config.deployment),
            reasoning_effort=self.config.reasoning_effort,
            max_output_tokens=self.config.max_output_tokens,
        )

    async def reason_about_market(
        self, row: RadarRow, *, evidence_context: list[dict[str, object]],
    ) -> CognitionResult:
        body = self.request_body(row, evidence_context=evidence_context)
        response = await self.client.post(
            responses_url(str(self.config.endpoint)),
            json=body,
            headers=self._headers,
        )
        response.raise_for_status()
        payload = response.json()
        raw_packet = json.loads(_output_text(payload))

        allowed_evidence = set(row.evidence_ids)
        packet_evidence = tuple(
            evidence_id
            for evidence_id in raw_packet["evidence_ids"]
            if evidence_id in allowed_evidence
        )
        packet = CognitionPacket(
            market_id=row.market_id,
            thesis=str(raw_packet["thesis"]),
            confidence=float(raw_packet["confidence"]),
            attention_reason=str(raw_packet["attention_reason"]),
            counterarguments=tuple(raw_packet["counterarguments"]),
            unknowns=tuple(raw_packet["unknowns"]),
            requested_research=tuple(raw_packet["requested_research"]),
            recommended_mode=str(raw_packet["recommended_mode"]),
            evidence_ids=packet_evidence,
        )
        input_tokens, output_tokens, total_tokens = _usage(payload)
        return CognitionResult(
            status="completed",
            packet=packet,
            detail=str(payload.get("id") or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
