"""Choose exactly one cognition provider; never silently fall back on failure."""

from __future__ import annotations

import os

from .cloudflare_config import CloudflareConfig
from .foundry_config import FoundryConfig
from .openai_config import OpenAIConfig

CognitionConfig = FoundryConfig | OpenAIConfig | CloudflareConfig


def cognition_config_from_env() -> CognitionConfig:
    provider = os.getenv("NOEMA_COGNITION_PROVIDER", "auto").strip().lower()
    if provider == "foundry":
        return FoundryConfig.from_env()
    if provider == "openai":
        return OpenAIConfig.from_env()
    if provider == "cloudflare":
        return CloudflareConfig.from_env()
    if provider == "auto":
        cloudflare = CloudflareConfig.from_env()
        if cloudflare.ready:
            return cloudflare
        openai = OpenAIConfig.from_env()
        if openai.ready:
            return openai
        foundry = FoundryConfig.from_env()
        if foundry.ready:
            return foundry
        return cloudflare
    raise ValueError("unsupported cognition provider")


def cognition_provider_name(config: CognitionConfig) -> str:
    if isinstance(config, OpenAIConfig):
        return "openai"
    if isinstance(config, CloudflareConfig):
        return "cloudflare_workers_ai"
    if isinstance(config, FoundryConfig):
        return "foundry"
    raise ValueError("unsupported cognition configuration")
