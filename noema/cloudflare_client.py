"""Evidence-only structured cognition over the Cloudflare Workers AI REST API."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from .cloudflare_config import CLOUDFLARE_API_ROOT, CloudflareConfig
from .cognition_models import CognitionResult
from .openai_client import _parse_packet
from .opportunity_radar import RadarRow


def _packet_response(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("response")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Cloudflare response did not contain a structured object")


def _usage(payload: dict[str, Any], result: dict[str, Any]) -> tuple[int, int, int]:
    usage = result.get("usage", payload.get("usage"))
    if not isinstance(usage, dict):
        raise TypeError("Cloudflare token usage unavailable")
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    total_tokens = usage.get("total_tokens")
    if (type(input_tokens) is not int or input_tokens < 0
            or type(output_tokens) is not int or output_tokens < 0):
        raise ValueError("Cloudflare token usage invalid")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    if type(total_tokens) is not int or total_tokens < input_tokens + output_tokens:
        raise ValueError("Cloudflare total token usage invalid")
    return input_tokens, output_tokens, total_tokens


class CloudflareCognitionClient:
    def __init__(
        self, config: CloudflareConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or CloudflareConfig.from_env()
        self.config.validate()
        if not self.config.ready:
            raise RuntimeError("Cloudflare Workers AI is not enabled and configured")
        self._headers = {
            "Authorization": f"Bearer {self.config.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "NOEMA/0.1",
        }
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.request_timeout_seconds),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def request_body(self, row: RadarRow, *, evidence_context: list[dict[str, object]]) -> dict:
        from .cognition_request import build_cognition_request

        return build_cognition_request(
            row, evidence_context=evidence_context, model=self.config.model,
            reasoning_effort="none", max_output_tokens=self.config.max_output_tokens,
        )

    async def structured_research(self, body: dict[str, Any]) -> dict[str, Any]:
        """Translate NOEMA's shared Responses-shaped request to Workers AI JSON mode."""
        text_format = body.get("text", {}).get("format", {})
        schema = text_format.get("schema")
        instructions = body.get("instructions")
        prompt = body.get("input")
        if (not isinstance(schema, dict) or not isinstance(instructions, str)
                or not isinstance(prompt, str)):
            raise TypeError("invalid structured cognition request")
        endpoint = (
            f"{CLOUDFLARE_API_ROOT}/accounts/{self.config.account_id}/ai/run/"
            f"{quote(self.config.model, safe='@/') }"
        )
        response = await self.client.post(
            endpoint,
            headers=self._headers,
            json={
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_schema", "json_schema": schema},
                "max_tokens": min(self.config.max_output_tokens,
                                   int(body.get("max_output_tokens", self.config.max_output_tokens))),
                "temperature": 0,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError("Cloudflare request was unsuccessful")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TypeError("Cloudflare response was invalid")
        structured = _packet_response(result)
        input_tokens, output_tokens, total_tokens = _usage(payload, result)
        return {"structured": structured, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "total_tokens": total_tokens}

    async def reason_about_market(
        self, row: RadarRow, *, evidence_context: list[dict[str, object]],
    ) -> CognitionResult:
        body = self.request_body(row, evidence_context=evidence_context)
        payload = await self.structured_research(body)
        packet = _parse_packet(payload["structured"], row)
        return CognitionResult(
            status="completed", packet=packet, detail=self.config.model,
            input_tokens=payload["input_tokens"], output_tokens=payload["output_tokens"],
            total_tokens=payload["total_tokens"],
        )
