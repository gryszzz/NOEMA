from datetime import UTC, datetime

import pytest

from noema.cognition_policy import CognitionPolicy, assess_cognition
from noema.cognition_store import CognitionStore
from noema.opportunity_radar import RadarRow


def row() -> RadarRow:
    return RadarRow(
        venue="kalshi",
        market_id="M",
        title="Test",
        probability_yes=0.65,
        market_probability=0.55,
        yes_ask=0.56,
        raw_edge=0.09,
        estimated_cost=0.01,
        uncertainty_penalty=0.02,
        robust_edge=0.06,
        spread=0.02,
        liquidity_usd=5000,
        captured_at=datetime.now(UTC).isoformat(),
        freshness_seconds=10,
        uncertainty_width=0.10,
        attention_score=0.80,
        decision="pass",
        reason="research",
        evidence_ids=("e1",),
    )


def test_good_market_can_trigger_cognition(tmp_path) -> None:
    store = CognitionStore(str(tmp_path / "noema.db"))
    result = assess_cognition(row(), store, CognitionPolicy())
    assert result.eligible is True


def test_low_attention_does_not_trigger(tmp_path) -> None:
    store = CognitionStore(str(tmp_path / "noema.db"))
    item = row()
    item = RadarRow(**{**item.__dict__, "attention_score": 0.20})
    result = assess_cognition(item, store, CognitionPolicy())
    assert result.eligible is False


def test_estimated_spend_is_reserved_across_connections(tmp_path) -> None:
    path = str(tmp_path / "budget.db")
    first, second = CognitionStore(path), CognitionStore(path)
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)
    assert first.reserve_estimated_cost(0.3, daily_limit_usd=0.5, now=now)
    assert not second.reserve_estimated_cost(0.3, daily_limit_usd=0.5, now=now)
    assert second.reserve_estimated_cost(
        0.3, daily_limit_usd=0.5, now=datetime(2026, 9, 27, 0, tzinfo=UTC)
    )


def test_model_budget_requires_price_and_uses_max_output() -> None:
    policy = CognitionPolicy()
    with pytest.raises(ValueError):
        policy.estimated_max_call_usd(input_bytes=2000, max_output_tokens=500)
    priced = CognitionPolicy(input_usd_per_million=1.10, output_usd_per_million=4.40)
    assert priced.estimated_max_call_usd(
        input_bytes=2000, max_output_tokens=500
    ) == pytest.approx(0.0044)
