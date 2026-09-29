from datetime import UTC, datetime, timedelta

import pytest

from noema.cognition import maybe_run_cognition
from noema.foundry_config import FoundryConfig
from noema.opportunity_radar import RadarRow
from noema.provenance import EvidenceStore


def row() -> RadarRow:
    return RadarRow(
        "kalshi:production", "SERIES-1-A", "Test market", 0.65, 0.55, 0.56,
        0.09, 0.01, 0.02, 0.06, 0.02, 5000,
        datetime.now(UTC).isoformat(), 5, 0.1, 0.85, "pass", "research", ("e1",),
    )


@pytest.mark.asyncio
async def test_cognition_is_safe_when_unconfigured(tmp_path) -> None:
    result = await maybe_run_cognition(
        [],
        db_path=str(tmp_path / "noema.db"),
        config=FoundryConfig(enabled=True),
    )
    assert result.status == "unconfigured"


@pytest.mark.asyncio
async def test_cognition_rejects_missing_verified_evidence_without_model_call(tmp_path) -> None:
    result = await maybe_run_cognition(
        [row()], db_path=str(tmp_path / "noema.db"),
        config=FoundryConfig(
            endpoint="https://resource.openai.azure.com", api_key="unused",
            deployment="reviewer",
        ),
    )
    assert result.status == "idle"
    assert "evidence" in (result.detail or "")


@pytest.mark.asyncio
async def test_cognition_does_not_call_paid_model_without_price_budget(tmp_path) -> None:
    db = str(tmp_path / "noema.db")
    EvidenceStore(db).append(
        evidence_id="e1", source="settlements", source_type="historical_outcomes",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        payload={"series": "SERIES", "events": 32, "yes": 17,
                 "markets_per_event": 1},
    )
    result = await maybe_run_cognition(
        [row()], db_path=db,
        config=FoundryConfig(
            endpoint="https://resource.openai.azure.com", api_key="unused",
            deployment="reviewer",
        ),
    )
    assert result.status == "idle"
    assert "budget" in (result.detail or "")


@pytest.mark.asyncio
async def test_invalid_usage_keeps_reservation_without_stopping_worker(tmp_path, monkeypatch):
    from decimal import Decimal

    from noema.bill_tracker import BillTracker
    from noema.cognition_models import CognitionPacket, CognitionResult
    from noema.cognition_policy import CognitionPolicy
    from noema.cognition_store import CognitionStore

    db = str(tmp_path / 'noema.db')
    BillTracker(db).configure(hosting_usd=Decimal(0), other_usd=Decimal(1),
                              owner_limit_usd=Decimal(1), model_budget_usd=Decimal(1))
    monkeypatch.setattr('noema.cognition.context_for_row', lambda *args: {})

    class Client:
        def __init__(self, config):
            pass

        def request_body(self, *args, **kwargs):
            return {'instructions': 'test', 'input': 'test'}

        async def reason_about_market(self, *args, **kwargs):
            packet = CognitionPacket('SERIES-1-A', 'test', .5, 'test', (), (), (), 'pass', ())
            return CognitionResult('completed', packet, input_tokens=10, total_tokens=1)

        async def close(self):
            pass

    monkeypatch.setattr('noema.cognition.FoundryCognitionClient', Client)
    result = await maybe_run_cognition(
        [row()], db_path=db,
        config=FoundryConfig(endpoint='https://example.invalid', api_key='unused', deployment='test'),
        policy=CognitionPolicy(input_usd_per_million=1, output_usd_per_million=1),
    )
    assert result.status == 'degraded'
    store = CognitionStore(db)
    assert store.latest() is None
    assert store.conn.execute('SELECT COUNT(*) FROM cognition_budget_reservations').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_openai_trace_is_attached_to_persisted_cognition_result(tmp_path, monkeypatch):
    from decimal import Decimal

    from noema.bill_tracker import BillTracker
    from noema.cognition_models import CognitionPacket, CognitionResult
    from noema.cognition_policy import CognitionPolicy
    from noema.cognition_store import CognitionStore
    from noema.openai_config import OpenAIConfig

    db = str(tmp_path / "noema.db")
    bills = BillTracker(db)
    bills.configure(
        hosting_usd=Decimal(0), other_usd=Decimal(2),
        model_budget_usd=Decimal(1), owner_limit_usd=Decimal(10),
    )
    bills.conn.close()
    monkeypatch.setattr("noema.cognition.context_for_row", lambda *_args: {})

    class Client:
        def __init__(self, _config):
            pass

        def request_body(self, *_args, **_kwargs):
            return {"input": "bounded fixture"}

        async def reason_about_market(self, *_args, trace_metadata=None, **_kwargs):
            assert trace_metadata["provider"] == "openai"
            assert trace_metadata["financial_mode"] == "research-only"
            assert trace_metadata["authority_state"] == "no-execution-authority"
            assert "market_id" not in trace_metadata
            packet = CognitionPacket(
                "SERIES-1-A", "test", .5, "test", (), (), (), "investigate", ("e1",),
            )
            return CognitionResult(
                "completed", packet, detail="response_fixture", input_tokens=10,
                output_tokens=3, total_tokens=13, trace_id="trace_fixture",
                trace_status="submitted",
            )

        async def close(self):
            pass

    monkeypatch.setattr("noema.cognition.OpenAICognitionClient", Client)
    result = await maybe_run_cognition(
        [row()], db_path=db,
        config=OpenAIConfig(api_key="test-only", model="test-model", enabled=True),
        policy=CognitionPolicy(
            input_usd_per_million=1, output_usd_per_million=1,
            max_estimated_usd_per_day=1,
        ),
    )

    assert result.status == "completed"
    assert result.decision_id
    assert result.trace_id == "trace_fixture"
    assert result.trace_status == "submitted"
    store = CognitionStore(db)
    latest = store.latest()
    assert latest is not None
    assert latest["decision_id"] == result.decision_id
    assert latest["trace_id"] == "trace_fixture"
    assert latest["trace_status"] == "submitted"
