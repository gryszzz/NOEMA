import json
from decimal import Decimal

import httpx
import pytest

from noema.bill_tracker import BillTracker
from noema.cognition_store import CognitionStore
from noema.economic_ledger import EconomicLedger
from noema.openai_client import OpenAICognitionClient
from noema.openai_config import OpenAIConfig
from noema.openai_usage import record_openai_response_usage


def test_openai_response_usage_is_attributed_persisted_idempotently_and_budgeted(
    tmp_path, monkeypatch,
):
    path = str(tmp_path / "usage.db")
    monkeypatch.setenv("NOEMA_OPENAI_PRICING_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("NOEMA_OPENAI_INPUT_USD_PER_MILLION", "0.20")
    monkeypatch.setenv("NOEMA_OPENAI_OUTPUT_USD_PER_MILLION", "1.20")
    bills = BillTracker(path)
    bills.configure(
        hosting_usd=Decimal("17.75"), other_usd=Decimal("0.05"),
        model_budget_usd=Decimal("0.05"), owner_limit_usd=Decimal("17.80"),
    )
    bills.conn.close()
    reservations = CognitionStore(path)
    assert reservations.reserve_estimated_cost(
        0.004, daily_limit_usd=0.01, monthly_limit_usd=0.05,
        estimated_tokens=1800, hourly_token_limit=5000,
        provider="openai", model="gpt-5.6-luna", activity_id="decision-1",
    )
    reservations.conn.close()

    payload = {
        "id": "resp_safe_id", "usage": {
            "input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
            "input_tokens_details": {"cached_tokens": 300},
        },
    }
    result = record_openai_response_usage(
        path, payload, model="gpt-5.6-luna", subsystem="market_cognition",
        decision_id="decision-1", trace_id="trace-safe", trace_status="submitted",
    )
    assert result["status"] == "recorded"
    assert result["estimated_api_cost_usd"] == "0.00032"
    assert result["remaining_model_budget_usd"] == "0.046"

    record_openai_response_usage(
        path, payload, model="gpt-5.6-luna", subsystem="market_cognition",
        decision_id="decision-1", trace_id="trace-safe", trace_status="submitted",
    )
    ledger = EconomicLedger(path)
    try:
        rows = ledger.conn.execute(
            "SELECT amount_usd,evidence_json FROM economic_events "
            "WHERE provider='openai' AND event_type='model_usage_cost_estimate'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "0.00032"
        evidence = json.loads(rows[0][1])
        assert evidence["subsystem"] == "market_cognition"
        assert evidence["cached_input_tokens"] == 300
        assert evidence["input_pricing"] == "uncached_rate_upper_bound"
        assert evidence["decision_id"] == "decision-1"
    finally:
        ledger.conn.close()


def test_openai_usage_cost_stays_unavailable_when_price_model_does_not_match(
    tmp_path, monkeypatch,
):
    path = str(tmp_path / "usage.db")
    monkeypatch.setenv("NOEMA_OPENAI_PRICING_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("NOEMA_OPENAI_INPUT_USD_PER_MILLION", "0.20")
    monkeypatch.setenv("NOEMA_OPENAI_OUTPUT_USD_PER_MILLION", "1.20")
    result = record_openai_response_usage(
        path, {"id": "resp_safe_id", "usage": {
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        }}, model="gpt-5.6-luna", subsystem="research_allocator",
        decision_id="decision-2", trace_id=None, trace_status="disabled",
    )
    assert result["status"] == "price_unavailable"
    assert not (tmp_path / "usage.db").exists()


@pytest.mark.asyncio
async def test_openai_client_persists_cost_before_rejecting_invalid_model_output(
    tmp_path, monkeypatch, capsys,
):
    path = str(tmp_path / "usage.db")
    monkeypatch.setenv("NOEMA_OPENAI_PRICING_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("NOEMA_OPENAI_INPUT_USD_PER_MILLION", "0.20")
    monkeypatch.setenv("NOEMA_OPENAI_OUTPUT_USD_PER_MILLION", "1.20")
    bills = BillTracker(path)
    bills.configure(
        hosting_usd=Decimal("17.75"), other_usd=Decimal("0.05"),
        model_budget_usd=Decimal("0.05"), owner_limit_usd=Decimal("17.80"),
    )
    bills.conn.close()

    def respond(_request):
        return httpx.Response(200, json={
            "id": "resp-invalid-packet", "status": "completed", "output": [],
            "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    client = OpenAICognitionClient(
        OpenAIConfig(api_key="secret-fixture", model="gpt-5.6-luna", enabled=True),
        http, usage_db_path=path, subsystem="research_allocator", decision_id="decision-3",
    )
    try:
        with pytest.raises(ValueError, match="no research packet"):
            await client.structured_research({"input": "private fixture prompt"})
    finally:
        await client.close()

    output = capsys.readouterr().out
    assert "secret-fixture" not in output
    assert "private fixture prompt" not in output
    assert '"subsystem": "research_allocator"' in output
    assert '"estimated_api_cost_usd": "0.000032"' in output
    ledger = EconomicLedger(path)
    try:
        row = ledger.conn.execute(
            "SELECT amount_usd FROM economic_events WHERE provider='openai' "
            "AND event_type='model_usage_cost_estimate'"
        ).fetchone()
        assert row is not None and row[0] == "0.000032"
    finally:
        ledger.conn.close()
