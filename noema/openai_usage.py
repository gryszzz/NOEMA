"""Per-response OpenAI usage attribution for budget-gated NOEMA workloads."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .bill_tracker import BillTracker
from .cognition_policy import CognitionPolicy
from .cognition_store import CognitionStore
from .economic_ledger import EconomicEvent, EconomicLedger


def record_openai_response_usage(
    db_path: str,
    payload: dict[str, Any],
    *,
    model: str,
    subsystem: str,
    decision_id: str | None,
    trace_id: str | None,
    trace_status: str,
) -> dict[str, Any]:
    """Persist token-backed cost estimates before parsing model output.

    Input is priced at the configured uncached rate, so cache discounts are not
    assumed. Provider invoices remain the reconciliation source of truth.
    """
    response_id = payload.get("id")
    usage = payload.get("usage")
    if not isinstance(response_id, str) or not response_id or not isinstance(usage, dict):
        return {"status": "usage_unavailable", "response_id_present": bool(response_id)}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if (
        any(type(value) is not int or value < 0
            for value in (input_tokens, output_tokens, total_tokens))
        or total_tokens < input_tokens + output_tokens
    ):
        return {"status": "usage_invalid", "response_id": response_id}

    policy = CognitionPolicy.from_env(provider="openai", model=model)
    if policy.input_usd_per_million is None or policy.output_usd_per_million is None:
        return {
            "status": "price_unavailable", "response_id": response_id,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        }
    input_rate = Decimal(str(policy.input_usd_per_million))
    output_rate = Decimal(str(policy.output_usd_per_million))
    amount = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / Decimal(1_000_000)
    if not amount.is_finite() or amount < 0:
        return {"status": "cost_invalid", "response_id": response_id}

    ledger = EconomicLedger(db_path)
    try:
        ledger.record_event(EconomicEvent(
            provider="openai", external_reference_id=f"openai-response:{response_id}",
            event_type="model_usage_cost_estimate", occurred_at=datetime.now(UTC),
            currency="USD", amount=amount, amount_usd=amount,
            reconciliation_state="ESTIMATED", value_state="realized",
            capital_class="cost", confidence_state="estimated",
            completeness_state="incomplete", lane="cognition", activity_id=decision_id,
            evidence={
                "response_id": response_id, "decision_id": decision_id,
                "subsystem": subsystem, "model": model,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cached_input_tokens": _cached_input_tokens(usage),
                "input_usd_per_million": str(input_rate),
                "output_usd_per_million": str(output_rate),
                "trace_id": trace_id, "trace_status": trace_status,
                "price_source": "configured model rate; provider invoice not reconciled",
                "input_pricing": "uncached_rate_upper_bound",
            },
        ))
    finally:
        ledger.conn.close()

    remaining: str | None = None
    bills = BillTracker(db_path)
    try:
        budget = bills.overview().get("model_budget_usd")
        if budget is not None:
            store = CognitionStore(db_path)
            try:
                remaining = str(store.monthly_model_budget_remaining(Decimal(budget)))
            finally:
                store.conn.close()
    finally:
        bills.conn.close()
    return {
        "status": "recorded", "response_id": response_id,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": total_tokens, "estimated_api_cost_usd": str(amount),
        "remaining_model_budget_usd": remaining,
        "remaining_basis": "monthly cap less conservative reservations",
        "subsystem": subsystem,
    }


def _cached_input_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        return None
    value = details.get("cached_tokens")
    return value if type(value) is int and value >= 0 else None
