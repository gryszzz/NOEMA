from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from noema.baseline_recording import record_market_baseline
from noema.bill_tracker import BillTracker
from noema.cognition_diagnostic import (
    _persisted_context,
    _request_body,
    run_owner_diagnostic,
)
from noema.cognition_store import CognitionStore
from noema.ledger import ForecastLedger
from noema.models import MarketSnapshot
from noema.openai_config import OpenAIConfig


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        "kalshi:production", "KXTEST-1-A", "Persisted observation", .42, .44,
        .43, .45, 1200, None, "rules observed", captured_at=datetime.now(UTC),
    )


def test_diagnostic_context_is_bounded_and_marks_execution_boundary(tmp_path):
    db = str(tmp_path / "noema.db")
    record_market_baseline(_snapshot(), ForecastLedger(db))

    context = _persisted_context(db)
    assert context["forecast_rows"]
    assert all(row["market_id"] == "KXTEST-1-A" for row in context["forecast_rows"])
    assert context["execution_class"] == "diagnostic/non-executable"
    body = _request_body(OpenAIConfig(
        api_key="not-used", model="gpt-5.6-luna", enabled=True,
        max_output_tokens=1500,
    ), context)
    assert body["max_output_tokens"] == 1000
    assert body["store"] is False
    assert "non-executable" in body["instructions"]


@pytest.mark.asyncio
async def test_owner_diagnostic_is_budgeted_persisted_and_idempotent(tmp_path, monkeypatch):
    db = str(tmp_path / "noema.db")
    record_market_baseline(_snapshot(), ForecastLedger(db))
    BillTracker(db).configure(
        hosting_usd=Decimal("16.75"), other_usd=Decimal("0.05"),
        model_budget_usd=Decimal("0.05"), owner_limit_usd=Decimal("16.80"),
    )
    env = {
        "NOEMA_COGNITION_DIAGNOSTIC_TRIGGER_ID": "owner-diagnostic-0001",
        "NOEMA_COGNITION_PROVIDER": "openai", "NOEMA_COGNITION_ENABLED": "1",
        "NOEMA_OPENAI_ENABLED": "1", "NOEMA_OPENAI_MODEL": "gpt-5.6-luna",
        "OPENAI_API_KEY": "test-key", "NOEMA_OPENAI_MAX_OUTPUT_TOKENS": "1000",
        "NOEMA_COGNITION_MAX_ESTIMATED_USD_PER_DAY": "0.01",
        "NOEMA_COGNITION_MAX_CALLS_PER_HOUR": "1",
        "NOEMA_COGNITION_MAX_TOKENS_PER_HOUR": "4000",
        "NOEMA_OPENAI_PRICING_MODEL": "gpt-5.6-luna",
        "NOEMA_OPENAI_INPUT_USD_PER_MILLION": "1",
        "NOEMA_OPENAI_OUTPUT_USD_PER_MILLION": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    result_payload = {
        "summary": "Persisted quote reviewed.", "findings": ["One quote exists."],
        "data_quality_blockers": ["No grounded outcome history."],
        "next_observation": "Collect further observations.",
        "supplied_evidence_ids": [], "action": "observe_only",
    }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def structured_research(self, body, **kwargs):
            assert body["max_output_tokens"] == 1000
            return {
                "id": "resp-test", "usage": {"input_tokens": 120,
                "output_tokens": 25, "total_tokens": 145},
                "status": "completed",
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": json.dumps(result_payload)},
                ]}],
            }

        async def close(self):
            return None

    monkeypatch.setattr("noema.cognition_diagnostic.OpenAICognitionClient", FakeClient)
    result = await run_owner_diagnostic(db)
    assert result["status"] == "completed"
    assert result["persisted"] is True
    assert result["input_tokens"] == 120
    assert result["output_tokens"] == 25
    assert result["result"]["action"] == "observe_only"
    assert result["model_budget_remaining_usd"] < "0.05"

    repeated = await run_owner_diagnostic(db)
    assert repeated["status"] == "already_consumed"

    store = CognitionStore(db)
    row = store.conn.execute(
        "SELECT status, execution_class, input_tokens, output_tokens, result_json "
        "FROM cognition_diagnostics WHERE trigger_id=?", (env["NOEMA_COGNITION_DIAGNOSTIC_TRIGGER_ID"],),
    ).fetchone()
    assert row[0] == "completed"
    assert row[1] == "non-executable"
    assert row[2:4] == (120, 25)
    assert json.loads(row[4])["action"] == "observe_only"
    assert store.conn.execute("SELECT COUNT(*) FROM cognition_budget_reservations").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_diagnostic_fails_closed_without_persisted_market_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_COGNITION_DIAGNOSTIC_TRIGGER_ID", "owner-diagnostic-0002")
    result = await run_owner_diagnostic(str(tmp_path / "missing.db"))
    assert result["status"] == "blocked"
    assert result["reason"] == "persisted evidence unavailable"
