from datetime import UTC, datetime, timedelta

from noema.grounding import GroundingPolicy, validate_forecast_grounding
from noema.models import Forecast
from noema.provenance import EvidenceStore


def test_missing_evidence_blocks_forecast(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "noema.db"))
    forecast = Forecast(
        market_id="M",
        venue="kalshi",
        probability_yes=0.6,
        lower_bound=0.5,
        upper_bound=0.7,
        model_version="m",
        evidence_ids=("missing",),
    )
    result = validate_forecast_grounding(forecast, store)
    assert result.grounded is False


def test_stale_evidence_blocks_forecast(tmp_path) -> None:
    store = EvidenceStore(str(tmp_path / "noema.db"))
    now = datetime.now(UTC)
    store.append(
        evidence_id="old",
        source="kalshi",
        source_type="market_data",
        observed_at=now - timedelta(minutes=10),
        payload={"price": 0.5},
        retrieved_at=now,
    )
    forecast = Forecast(
        market_id="M",
        venue="kalshi",
        probability_yes=0.6,
        lower_bound=0.5,
        upper_bound=0.7,
        model_version="m",
        evidence_ids=("old",),
    )
    result = validate_forecast_grounding(
        forecast,
        store,
        policy=GroundingPolicy(max_evidence_age_seconds=60),
        now=now,
    )
    assert result.grounded is False
