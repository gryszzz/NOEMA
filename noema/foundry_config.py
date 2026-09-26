from __future__ import annotations

import os
from dataclasses import dataclass


_ALLOWED_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


@dataclass(frozen=True)
class FoundryConfig:
    endpoint: str | None = None
    api_key: str | None = None
    deployment: str | None = None
    enabled: bool = True
    reasoning_effort: str = "medium"
    request_timeout_seconds: float = 60.0
    max_output_tokens: int = 1500

    @classmethod
    def from_env(cls) -> "FoundryConfig":
        return cls(
            endpoint=os.getenv("NOEMA_FOUNDRY_ENDPOINT"),
            api_key=os.getenv("NOEMA_FOUNDRY_API_KEY"),
            deployment=os.getenv("NOEMA_FOUNDRY_DEPLOYMENT"),
            enabled=os.getenv("NOEMA_COGNITION_ENABLED", "1") == "1",
            reasoning_effort=os.getenv(
                "NOEMA_FOUNDRY_REASONING_EFFORT",
                "medium",
            ),
            request_timeout_seconds=float(
                os.getenv("NOEMA_FOUNDRY_TIMEOUT_SECONDS", "60")
            ),
            max_output_tokens=int(
                os.getenv("NOEMA_FOUNDRY_MAX_OUTPUT_TOKENS", "1500")
            ),
        )

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled
            and self.endpoint
            and self.api_key
            and self.deployment
        )

    def validate(self) -> None:
        if self.reasoning_effort not in _ALLOWED_EFFORTS:
            raise ValueError("unsupported Foundry reasoning effort")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Foundry timeout must be positive")
        if self.max_output_tokens <= 0:
            raise ValueError("Foundry max output tokens must be positive")
        if self.endpoint and not self.endpoint.startswith(("https://", "http://")):
            raise ValueError("Foundry endpoint must be an HTTP(S) URL")


def responses_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/openai/v1"):
        return f"{base}/responses"
    return f"{base}/openai/v1/responses"
