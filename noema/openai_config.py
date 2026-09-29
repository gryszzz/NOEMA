"""Explicit opt-in configuration for the official OpenAI Responses API."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_ALLOWED_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


@dataclass(frozen=True)
class OpenAIConfig:
    api_key: str | None = field(default=None, repr=False)
    model: str | None = None
    project_id: str | None = None
    enabled: bool = False
    tracing_enabled: bool = True
    reasoning_effort: str = "medium"
    request_timeout_seconds: float = 60.0
    max_output_tokens: int = 1500

    @classmethod
    def from_env(cls) -> OpenAIConfig:
        return cls(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=os.getenv("NOEMA_OPENAI_MODEL"),
            project_id=os.getenv("NOEMA_OPENAI_PROJECT_ID") or os.getenv("OPENAI_PROJECT_ID"),
            enabled=(os.getenv("NOEMA_COGNITION_ENABLED", "1") == "1"
                     and os.getenv("NOEMA_OPENAI_ENABLED", "0") == "1"),
            tracing_enabled=os.getenv("NOEMA_OPENAI_TRACING", "1") == "1",
            reasoning_effort=os.getenv("NOEMA_OPENAI_REASONING_EFFORT", "medium"),
            request_timeout_seconds=float(os.getenv("NOEMA_OPENAI_TIMEOUT_SECONDS", "60")),
            max_output_tokens=int(os.getenv("NOEMA_OPENAI_MAX_OUTPUT_TOKENS", "1500")),
        )

    @property
    def ready(self) -> bool:
        return self.enabled and all(
            isinstance(value, str) and bool(value.strip())
            for value in (self.api_key, self.model)
        )

    def validate(self) -> None:
        if self.reasoning_effort not in _ALLOWED_EFFORTS:
            raise ValueError("unsupported OpenAI reasoning effort")
        if (not math.isfinite(self.request_timeout_seconds)
                or self.request_timeout_seconds <= 0):
            raise ValueError("OpenAI timeout must be positive and finite")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("OpenAI max output tokens must be a positive integer")
        if self.model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,199}", self.model):
            raise ValueError("invalid OpenAI model identifier")
        if self.project_id and not re.fullmatch(r"proj_[A-Za-z0-9_-]+", self.project_id):
            raise ValueError("invalid OpenAI project identifier")

    @property
    def supports_reasoning_effort(self) -> bool:
        """GPT-4.1/4o models are non-reasoning models and reject no-op effort settings."""
        return not (self.model or "").startswith(("gpt-4.1", "gpt-4o"))
