from noema.models import Forecast, MarketSnapshot, Mode, Opportunity
from noema.risk import RiskEngine, RiskPolicy


def opportunity(edge: float = 0.05) -> Opportunity:
    market = MarketSnapshot(
        venue="test",
        market_id="m1",
        title="Test market",
        yes_bid=0.50,
        yes_ask=0.52,
        no_bid=0.47,
        no_ask=0.49,
        liquidity_usd=10_000,
        closes_at=None,
        resolution_rules="Objective binary resolution.",
    )
    forecast = Forecast(
        market_id="m1",
        venue="test",
        probability_yes=0.62,
        lower_bound=0.58,
        upper_bound=0.66,
        model_version="test",
    )
    return Opportunity(
        forecast=forecast,
        snapshot=market,
        market_probability=0.52,
        raw_edge=0.10,
        estimated_cost=0.02,
        uncertainty_penalty=0.03,
        robust_edge=edge,
    )


def test_paper_mode_is_default() -> None:
    action = RiskEngine(RiskPolicy()).decide(opportunity(), bankroll_usd=2_000)
    assert action.decision.value == "paper_buy_yes"
    assert action.stake_usd <= 10


def test_small_edge_passes() -> None:
    action = RiskEngine(RiskPolicy()).decide(opportunity(0.01), bankroll_usd=2_000)
    assert action.decision.value == "pass"


def test_live_mode_is_explicit() -> None:
    action = RiskEngine(RiskPolicy(mode=Mode.LIVE)).decide(opportunity(), bankroll_usd=2_000)
    assert action.decision.value == "live_buy_yes"


def test_external_risk_multiplier_scales_stake() -> None:
    engine = RiskEngine(RiskPolicy())
    full = engine.decide(opportunity(), bankroll_usd=2_000, risk_multiplier=1.0)
    half = engine.decide(opportunity(), bankroll_usd=2_000, risk_multiplier=0.5)
    assert half.stake_usd == full.stake_usd / 2


def test_external_zero_multiplier_halts() -> None:
    action = RiskEngine(RiskPolicy()).decide(
        opportunity(),
        bankroll_usd=2_000,
        risk_multiplier=0.0,
    )
    assert action.decision.value == "pass"
    assert action.stake_usd == 0
