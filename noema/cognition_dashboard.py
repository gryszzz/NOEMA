from __future__ import annotations

import os
from typing import Any

import httpx

from .bill_tracker import BillTracker
from .cloudflare_config import CLOUDFLARE_API_ROOT, CloudflareConfig
from .cognition_config import cognition_config_from_env, cognition_provider_name
from .cognition_store import CognitionStore
from .local_cognition import (
    LocalCognitionConfig,
    max_local_model_size_gib,
    model_size_bytes,
)
from .openai_config import OpenAIConfig
from .research_queue import ResearchQueueStore


def _health(url: str) -> dict[str, Any]:
    """Read local specialist health without returning response bodies or secrets."""
    try:
        response = httpx.get(url, timeout=1.5, follow_redirects=False)
        if response.status_code != 200:
            return {"status": "unhealthy", "http_status": response.status_code}
        payload = response.json()
        if not isinstance(payload, dict):
            return {"status": "invalid_response"}
        return {"status": "healthy", **{
            key: str(payload[key])[:120] for key in ("model", "ok") if key in payload
        }}
    except (httpx.HTTPError, ValueError):
        return {"status": "unavailable"}


def _runtime_providers(config: Any) -> dict[str, Any]:
    local = LocalCognitionConfig.from_env()
    try:
        response = httpx.get(local.endpoint + "/models", timeout=1.5, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        models = []
        selected_details = None
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            model_id = row["id"][:200]
            details = row.get("dmr") if isinstance(row.get("dmr"), dict) else {}
            if model_id == local.model:
                selected_details = {"size": details.get("size", row.get("size"))}
            architecture = str(details.get("architecture", "unknown"))[:40]
            lower = model_id.lower()
            capabilities = []
            if "embedding" in lower or architecture == "nomic-bert":
                capabilities.append("embedding")
            elif "reranker" in lower:
                capabilities.append("reranker_model")
            else:
                capabilities.append("chat")
            if "moondream" in lower:
                capabilities.append("vision_model")
            models.append({"id": model_id, "architecture": architecture,
                           "size": str(details.get("size", row.get("size", "unknown")))[:40],
                           "capabilities": capabilities})
        selected_size = None if selected_details is None else model_size_bytes(
            selected_details.get("size"),
        )
        max_model_gib = max_local_model_size_gib()
        selected_available = any(item["id"] == local.model for item in models)
        if not selected_available:
            selected_reason = "configured local model is not installed"
        elif selected_size is None:
            selected_reason = "RESOURCE LIMITED: local model size is unavailable"
        elif selected_size > max_model_gib * 1024**3:
            selected_reason = ("RESOURCE LIMITED: configured local model exceeds the "
                               f"{max_model_gib:g} GiB model size ceiling")
        else:
            selected_reason = None
        local_status = {
            "status": "healthy", "endpoint": local.endpoint,
            "selected_model": local.model,
            "selected_model_available": selected_available,
            "selected_model_size_bytes": selected_size,
            "selected_model_resource_reason": selected_reason,
            "selected_model_resource_eligible": (
                selected_available and selected_size is not None
                and selected_size <= max_model_gib * 1024**3
            ),
            "model_size_ceiling_gib": max_model_gib,
            "models": models,
        }
    except (httpx.HTTPError, ValueError):
        local_status = {"status": "unavailable", "endpoint": local.endpoint,
                        "selected_model": local.model, "models": []}

    services = {}
    for name, variable, default in (
        ("chronos", "NOEMA_CHRONOS_URL", "http://127.0.0.1:8011"),
        ("finbert", "NOEMA_FINBERT_URL", "http://127.0.0.1:8012"),
    ):
        base = os.getenv(variable, default).rstrip("/")
        services[name] = {"endpoint": base, **_health(base + "/health")}

    openai_key = bool(os.getenv("OPENAI_API_KEY", "").strip())
    openai = OpenAIConfig.from_env()
    groq_key = bool(os.getenv("GROQ_API_KEY", "").strip())
    cloudflare = CloudflareConfig.from_env()
    cloudflare_token = bool(cloudflare.api_token and cloudflare.api_token.strip())
    cloudflare_account = bool(cloudflare.account_id and cloudflare.account_id.strip())
    cloudflare_health: dict[str, Any] = {
        "status": "credential_missing" if not cloudflare_token or not cloudflare_account
        else "disabled" if not cloudflare.enabled else "unavailable",
        "credential_present": cloudflare_token and cloudflare_account,
        "model": cloudflare.model,
        "model_available": False,
    }
    if cloudflare.ready:
        url = (f"{CLOUDFLARE_API_ROOT}/accounts/{cloudflare.account_id}/ai/models/search")
        try:
            response = httpx.get(
                url, params={"search": cloudflare.model, "per_page": 10},
                headers={"Authorization": f"Bearer {cloudflare.api_token}",
                         "Accept": "application/json"},
                timeout=3.0, follow_redirects=False,
            )
            cloudflare_health["http_status"] = response.status_code
            if response.status_code in {401, 403}:
                cloudflare_health["status"] = "authentication_rejected"
            elif response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is True:
                    models = payload.get("result")
                    if not isinstance(models, list):
                        models = payload.get("data")
                    if isinstance(models, list):
                        cloudflare_health["model_available"] = any(
                            isinstance(row, dict) and cloudflare.model in {
                                row.get("name"), row.get("id"), row.get("model")
                            }
                            for row in models
                        )
                        cloudflare_health["status"] = (
                            "healthy" if cloudflare_health["model_available"]
                            else "model_unavailable"
                        )
                    else:
                        cloudflare_health["status"] = "invalid_response"
                else:
                    cloudflare_health["status"] = "authentication_or_account_rejected"
            else:
                cloudflare_health["status"] = "unhealthy"
        except (httpx.HTTPError, ValueError):
            cloudflare_health["status"] = "unavailable"
    hosted = {
        "openai": {"status": "ready" if openai.ready else "credential_missing" if not openai_key
                   else "disabled_or_model_missing", "credential_present": openai_key},
        "groq": {"status": "credential_present_unwired" if groq_key else "credential_missing",
                 "credential_present": groq_key},
        "cloudflare_workers_ai": cloudflare_health,
    }
    specialists_status = "healthy" if any(
        item.get("status") == "healthy" for item in services.values()
    ) else "unavailable"
    hosted_status = "configured" if any(
        item.get("credential_present") for item in hosted.values()
    ) else "credential_missing"
    return {"local_model_runner": local_status,
            "specialists": {"status": specialists_status, **services},
            "hosted_providers": {"status": hosted_status, **hosted}}


def build_provider_health() -> dict[str, Any]:
    """Read provider readiness without creating stores or invoking inference."""
    config = cognition_config_from_env()
    runtime = _runtime_providers(config)
    hosted = runtime["hosted_providers"]
    local = runtime["local_model_runner"]
    services = runtime["specialists"]
    return {
        "configured_provider": cognition_provider_name(config),
        "configured_provider_ready": bool(config.ready),
        "cloudflare": {key: hosted["cloudflare_workers_ai"].get(key) for key in (
            "status", "model", "model_available", "credential_present",
        )},
        "openai": {key: hosted["openai"].get(key) for key in (
            "status", "credential_present",
        )},
        "docker_model_runner": {key: local.get(key) for key in (
            "status", "selected_model", "selected_model_resource_eligible",
            "selected_model_resource_reason", "model_size_ceiling_gib",
        )},
        "chronos": {key: services["chronos"].get(key) for key in ("status",)},
        "finbert": {key: services["finbert"].get(key) for key in ("status",)},
    }


def build_cognition_overview(path: str = "data/noema.db") -> dict[str, Any]:
    config = cognition_config_from_env()
    store = CognitionStore(path)
    queue = ResearchQueueStore(path)
    pending = queue.pending(limit=10)
    bill = BillTracker(path)
    try:
        bill_overview = bill.overview()
    finally:
        bill.conn.close()
    monthly_model_budget = bill_overview.get("model_budget_usd")
    return {
        "enabled": config.enabled,
        "configured": config.ready,
        "provider": cognition_provider_name(config),
        "deployment": (config.model if isinstance(config, (OpenAIConfig, CloudflareConfig))
                       else config.deployment),
        "credential_present": (
            bool(config.api_token and config.account_id)
            if isinstance(config, CloudflareConfig) else bool(config.api_key)
        ),
        "reasoning_effort": getattr(config, "reasoning_effort", None),
        "calls_last_hour": store.calls_last_hour(),
        "tokens_last_hour": store.tokens_last_hour(),
        "latest": store.latest(),
        "pending_research_count": queue.pending_count(),
        "pending_research": [task.__dict__ for task in pending],
        "runtime_providers": _runtime_providers(config),
        "hosted_cost_gate": {
            "status": bill_overview["status"],
            "monthly_model_budget_configured": (
                monthly_model_budget is not None and float(monthly_model_budget) > 0
            ),
        },
    }
