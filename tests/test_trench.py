from datetime import UTC, datetime, timedelta

from noema.trench_features import extract_trench_features
from noema.trench_models import LaunchTick, TokenControlState
from noema.trench_risk import assess_trench_candidate


def _ticks() -> list[LaunchTick]:
    start = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    return [
        LaunchTick(
            observed_at=start,
            price_usd=0.001,
            liquidity_usd=10_000,
            buy_volume_usd=100,
            sell_volume_usd=50,
            unique_buyers=10,
            unique_sellers=4,
            holder_shares=(0.08, 0.07, 0.06, 0.05, 0.04),
            creator_supply_fraction=0.02,
            organic_score=50,
            wash_trade_probability=0.05,
        ),
        LaunchTick(
            observed_at=start + timedelta(minutes=2),
            price_usd=0.0014,
            liquidity_usd=16_000,
            buy_volume_usd=250,
            sell_volume_usd=70,
            unique_buyers=35,
            unique_sellers=10,
            holder_shares=(0.07, 0.06, 0.05, 0.04, 0.03),
            creator_supply_fraction=0.02,
            organic_score=60,
            wash_trade_probability=0.05,
        ),
        LaunchTick(
            observed_at=start + timedelta(minutes=5),
            price_usd=0.0019,
            liquidity_usd=25_000,
            buy_volume_usd=550,
            sell_volume_usd=100,
            unique_buyers=100,
            unique_sellers=15,
            holder_shares=(0.06, 0.05, 0.04, 0.03, 0.02),
            creator_supply_fraction=0.02,
            organic_score=72,
            wash_trade_probability=0.05,
        ),
    ]


def test_trench_features_capture_growth_and_concentration() -> None:
    features = extract_trench_features(_ticks())

    assert features.age_seconds == 300
    assert features.return_fraction > 0
    assert features.liquidity_growth_fraction > 1
    assert features.buyer_growth_fraction > 5
    assert features.buyer_acceleration > 0
    assert features.top5_holder_fraction == 0.20


def test_clean_growth_can_become_research_candidate() -> None:
    ticks = _ticks()
    features = extract_trench_features(ticks)
    assessment = assess_trench_candidate(
        features,
        TokenControlState(),
        current_liquidity_usd=ticks[-1].liquidity_usd,
    )

    assert assessment.disposition == "research_candidate"
    assert assessment.survival_risk < 0.35
    assert assessment.opportunity_score >= 0.55


def test_control_and_wash_risk_quarantine_candidate() -> None:
    ticks = _ticks()
    risky_last = LaunchTick(
        **{
            **ticks[-1].__dict__,
            "wash_trade_probability": 0.95,
        }
    )
    features = extract_trench_features([*ticks[:-1], risky_last])
    assessment = assess_trench_candidate(
        features,
        TokenControlState(
            freeze_authority_present=True,
            permanent_delegate_present=True,
        ),
        current_liquidity_usd=risky_last.liquidity_usd,
    )

    assert assessment.disposition == "quarantine"
    assert assessment.survival_risk >= 0.75
