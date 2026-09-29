"""Cloudflare Workers AI configuration for bounded, structured cognition."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

CLOUDFLARE_API_ROOT = "https://api.cloudflare.com/client/v4"
DEFAULT_CLOUDFLARE_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


@dataclass(frozen=True)
class CloudflareConfig:
    api_token: str | None = field(default=None, repr=False)
    account_id: str | None = field(default=None, repr=False)
    model: str = DEFAULT_CLOUDFLARE_MODEL
    enabled: bool = False
    request_timeout_seconds: float = 60.0
    max_output_tokens: int = 1500

    @classmethod
    def from_env(cls) -> CloudflareConfig:
        token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip() or None
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip() or None
        credentials_present = bool(token and token.strip() and account_id and account_id.strip())
        return cls(
            api_token=token,
            account_id=account_id,
            model=os.getenv("NOEMA_CLOUDFLARE_MODEL", DEFAULT_CLOUDFLARE_MODEL),
            enabled=(os.getenv("NOEMA_COGNITION_ENABLED", "1") == "1"
                     and os.getenv("NOEMA_CLOUDFLARE_ENABLED", "1") == "1"
                     and credentials_present),
            request_timeout_seconds=float(
                os.getenv("NOEMA_CLOUDFLARE_TIMEOUT_SECONDS", "60")
            ),
            max_output_tokens=int(os.getenv("NOEMA_CLOUDFLARE_MAX_OUTPUT_TOKENS", "1500")),
        )

    @property
    def ready(self) -> bool:
        return self.enabled and all(
            isinstance(value, str) and bool(value.strip())
            for value in (self.api_token, self.account_id, self.model)
        )

    def validate(self) -> None:
        if (not math.isfinite(self.request_timeout_seconds)
                or self.request_timeout_seconds <= 0):
            raise ValueError("Cloudflare timeout must be positive and finite")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("Cloudflare max output tokens must be positive")
        if self.account_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.account_id):
            raise ValueError("invalid Cloudflare account identifier")
        if self.model and not re.fullmatch(r"@(cf|hf)/[A-Za-z0-9._/-]{1,180}", self.model):
            raise ValueError("invalid Cloudflare model identifier")
