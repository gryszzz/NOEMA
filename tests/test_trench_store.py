from datetime import UTC, datetime, timedelta

from noema.trench_features import extract_trench_features
from noema.trench_models import LaunchTick, TokenControlState
from noema.trench_risk import assess_trench_candidate
from noema.trench_store import TrenchResearchStore


def test_rejected_candidates_keep_counterfactual_outcomes(tmp_path) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    ticks = [
        LaunchTick(now, 1.0, 10_000, 50, 50, 5, 5),
        LaunchTick(
            now + timedelta(minutes=5),
            0.7,
            3_000,
            50,
            200,
            6,
            20,
            wash_trade_probability=0.9,
        ),
    ]
    features = extract_trench_features(ticks)
    assessment = assess_trench_candidate(
        features,
        TokenControlState(permanent_delegate_present=True),
        current_liquidity_usd=3_000,
    )
    assert assessment.disposition == "quarantine"

    store = TrenchResearchStore(str(tmp_path / "trench.db"))
    candidate_id = store.record_candidate(
        token_mint="Mint111",
        reference_price_usd=0.7,
        reference_liquidity_usd=3_000,
        assessment=assessment,
        captured_at=now + timedelta(minutes=5),
    )
    store.record_counterfactual(
        candidate_id=candidate_id,
        horizon_seconds=3600,
        final_return_fraction=-0.7,
        max_return_fraction=0.1,
        max_drawdown_fraction=0.8,
        observed_at=now + timedelta(hours=1, minutes=5),
    )

    stats = store.rejection_stats(horizon_seconds=3600)
    assert stats["rejected"] == 1
    assert stats["half_drawdown_rate"] == 1.0
    assert stats["missed_2x_rate"] == 0.0
