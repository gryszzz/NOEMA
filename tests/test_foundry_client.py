import json
from datetime import UTC, datetime

import httpx
import pytest

from noema.foundry_client import FoundryCognitionClient
from noema.foundry_config import FoundryConfig
from noema.opportunity_radar import RadarRow


def row() -> RadarRow:
    return RadarRow(
        venue="kalshi",
        market_id="M",
        title="Test market",
        probability_yes=0.65,
        market_probability=0.55,
        yes_ask=0.56,
        raw_edge=0.09,
        estimated_cost=0.01,
        uncertainty_penalty=0.02,
        robust_edge=0.06,
        spread=0.02,
        liquidity_usd=5000,
        captured_at=datetime.now(UTC).isoformat(),
        freshness_seconds=5,
        uncertainty_width=0.10,
        attention_score=0.85,
        decision="pass",
        reason="research",
        evidence_ids=("e1",),
    )


@pytest.mark.asyncio
async def test_foundry_uses_structured_responses_and_filters_evidence() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("api-key")
        body = json.loads(request.content)
        seen["body"] = body
        packet = {
            "thesis": "Investigate the apparent edge.",
            "confidence": 0.72,
            "attention_reason": "edge survives current penalties",
            "counterarguments": ["spread may widen"],
            "unknowns": ["external facts were not supplied"],
            "requested_research": ["verify evidence source"],
            "recommended_mode": "investigate",
            "evidence_ids": ["e1", "invented-e2"],
        }
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(packet),
                            }
                        ]
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "total_tokens": 140,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    client = FoundryCognitionClient(
        FoundryConfig(
            endpoint="https://resource.openai.azure.com",
            api_key="secret",
            deployment="astra-deploy",
            reasoning_effort="high",
        ),
        client=http,
    )
    try:
        result = await client.reason_about_market(row())
    finally:
        await client.close()

    assert seen["url"] == (
        "https://resource.openai.azure.com/openai/v1/responses"
    )
    assert seen["api_key"] == "secret"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "astra-deploy"
    assert body["reasoning"]["effort"] == "high"
    assert body["text"]["format"]["type"] == "json_schema"
    assert result.packet is not None
    assert result.packet.evidence_ids == ("e1",)
    assert result.total_tokens == 140
