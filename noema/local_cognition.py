"""Model-independent local structured cognition via Docker Model Runner."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

DEFAULT_MAX_MODEL_SIZE_GIB = 1.5
HARD_MAX_MODEL_SIZE_GIB = 4.0


def max_local_model_size_gib() -> float:
    """Return a bounded local model ceiling, falling back safely on bad config."""
    try:
        value = float(os.getenv("NOEMA_LOCAL_MODEL_MAX_SIZE_GIB", str(DEFAULT_MAX_MODEL_SIZE_GIB)))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_MODEL_SIZE_GIB
    if not 0 < value <= HARD_MAX_MODEL_SIZE_GIB:
        value = DEFAULT_MAX_MODEL_SIZE_GIB
    return value


def model_size_bytes(value: object) -> int | None:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|KiB|MiB|GiB)\s*",
                         str(value), re.IGNORECASE)
    if not match:
        return None
    unit = match.group(2).lower()
    scale = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3,
             "kib": 1024, "mib": 1024**2, "gib": 1024**3}[unit]
    return int(float(match.group(1)) * scale)


@dataclass(frozen=True)
class LocalCognitionConfig:
    model: str = "docker.io/ai/gemma3:latest"
    endpoint: str = "http://127.0.0.1:12434/engines/v1"

    @classmethod
    def from_env(cls) -> LocalCognitionConfig:
        return cls(model=os.getenv("NOEMA_LOCAL_MODEL", cls.model),
                   endpoint=os.getenv("NOEMA_LOCAL_ENDPOINT", cls.endpoint).rstrip("/"))

    def validate(self) -> None:
        url = urlsplit(self.endpoint)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"}
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("local cognition must use an unauthenticated loopback runner")
        if not self.model.strip() or len(self.model) > 200:
            raise ValueError("invalid local model")


class LocalCognitionClient:
    def __init__(self, config: LocalCognitionConfig | None = None,
                 client: httpx.AsyncClient | None = None):
        self.config = config or LocalCognitionConfig.from_env()
        self.config.validate()
        self.client = client or httpx.AsyncClient(timeout=120, follow_redirects=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def resource_eligibility(self) -> tuple[bool, str | None]:
        """Check installed size before loading a model on the constrained control node."""
        available = await self.client.get(self.config.endpoint + "/models")
        available.raise_for_status()
        payload = available.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return False, "RESOURCE LIMITED: local model metadata is unavailable"
        row = next((item for item in rows if isinstance(item, dict)
                    and item.get("id") == self.config.model), None)
        if row is None:
            return False, "configured local model is not installed"
        details = row.get("dmr") if isinstance(row.get("dmr"), dict) else {}
        size = details.get("size", row.get("size"))
        size_bytes = model_size_bytes(size)
        if size_bytes is None:
            return False, "RESOURCE LIMITED: local model size is unavailable"
        max_gib = max_local_model_size_gib()
        if size_bytes > max_gib * 1024**3:
            return False, (f"RESOURCE LIMITED: configured local model exceeds the "
                           f"{max_gib:g} GiB model size ceiling")
        return True, None

    async def structured_research(self, instructions: str, inputs: dict, schema: dict) -> dict:
        eligible, reason = await self.resource_eligibility()
        if not eligible:
            raise ValueError(reason or "local model resource admission failed")
        response = await self.client.post(self.config.endpoint + "/chat/completions", json={
            "model": self.config.model, "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(inputs, sort_keys=True)},
            ], "temperature": 0, "max_tokens": 512,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "noema_research_selection", "strict": True, "schema": schema,
            }},
        })
        response.raise_for_status()
        payload = response.json()
        text = payload["choices"][0]["message"]["content"].strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        return {"selection": json.loads(text), "usage": payload.get("usage")}
