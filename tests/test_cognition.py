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
