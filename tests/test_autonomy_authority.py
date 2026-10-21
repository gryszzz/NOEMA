from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from noema.autonomy import earned_autonomy
from noema.autonomy_authority import (
    authority_for,
    authority_table,
    effective_daily_cap,
    live_permission,
    load_authority_table,
    risk_policy_for_level,
)
from noema.autonomy_transition import transition_autonomy
from noema.economic_models import AutonomyEvidence, AutonomyLevel
from noema.execution_gateway import ExecutionGateway
from noema.models import Decision, Forecast, MarketSnapshot, Mode, Opportunity
from noema.risk import RiskEngine, RiskPolicy
from noema.trading_coordinator import TradingCoordinator


def _flags(monkeypatch, *, review: str = "0", halt: str = "0", daily: str | None = None) -> None:
    monkeypatch.setenv("NOEMA_EXECUTION_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    monkeypatch.setenv("NOEMA_MASTER_HALT", halt)
    monkeypatch.setenv("NOEMA_LIVE_VENUES", "kalshi:production")
    monkeypatch.setenv("NOEMA_LIVE_REQUIRES_OWNER_REVIEW", review)
    if daily is None:
        monkeypatch.delenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", raising=False)
    else:
        monkeypatch.setenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", daily)


def _authority(level, bankroll="1000"):
    return authority_for(
        level, Decimal(bankroll), mission_id="prediction-markets",
        allowed_venues=frozenset({"kalshi:production"}),
        allowed_instruments=frozenset({"m1"}),
    )


def test_table_maps_live_permission_and_bankroll_fractions() -> None:
    table = authority_table()
    assert table[AutonomyLevel.SHADOW].live is False
    assert table[AutonomyLevel.PAPER].live is False
    assert table[AutonomyLevel.DEMO].live is False
    assert table[AutonomyLevel.MICRO].per_trade_fraction == Decimal("0.01")
    assert table[AutonomyLevel.MICRO].daily_notional_fraction == Decimal("0.10")
    assert table[AutonomyLevel.PROVEN].per_trade_fraction == Decimal("0.03")
    assert table[AutonomyLevel.PROVEN].daily_notional_fraction == Decimal("0.30")
    assert table[AutonomyLevel.SELF_FUNDED].per_trade_fraction == Decimal("0.05")
    assert table[AutonomyLevel.SELF_FUNDED].daily_notional_fraction == Decimal("0.50")
    assert table[AutonomyLevel.EXPANSION].live is True
    assert table[AutonomyLevel.EXPANSION].per_trade_fraction == Decimal("0.08")


def test_authority_fractions_come_from_config(monkeypatch, tmp_path) -> None:
    path = tmp_path / "autonomy_authority.toml"
    path.write_text(
        '[micro]\nlive = true\nper_trade_fraction = "0.02"\ndaily_notional_fraction = "0.20"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("NOEMA_AUTONOMY_AUTHORITY_CONFIG", str(path))
    table = load_authority_table(str(path))
    assert table[AutonomyLevel.MICRO].per_trade_fraction == Decimal("0.02")
    assert AutonomyLevel.PROVEN not in table


def test_missing_level_and_missing_config_do_not_grant_live(monkeypatch, tmp_path) -> None:
    permitted, reasons = live_permission(None)
    assert permitted is False
    assert "absent" in reasons[0]
    assert authority_for(
        None, Decimal(1000), mission_id="prediction-markets",
        allowed_venues=frozenset({"kalshi:production"}),
        allowed_instruments=frozenset({"m1"}),
    ) is None
    missing = tmp_path / "missing.toml"
    monkeypatch.setenv("NOEMA_AUTONOMY_AUTHORITY_CONFIG", str(missing))
    _flags(monkeypatch)
    assert live_permission(AutonomyLevel.EXPANSION)[0] is False


def test_owner_review_flag_blocks_and_releases_live(monkeypatch) -> None:
    _flags(monkeypatch, review="1")
    blocked, reasons = live_permission(AutonomyLevel.MICRO)
    assert blocked is False
    assert "owner review" in reasons[0]
    assert _authority(AutonomyLevel.MICRO) is None

    _flags(monkeypatch, review="0")
    authority = _authority(AutonomyLevel.MICRO)
    assert authority is not None
    assert authority.per_action_limit_usd == Decimal("10.00")
    assert authority.daily_limit_usd == Decimal("100.00")


def test_open_review_still_requires_gateway_flags(monkeypatch) -> None:
    monkeypatch.setenv("NOEMA_LIVE_REQUIRES_OWNER_REVIEW", "0")
    monkeypatch.setenv("NOEMA_MASTER_HALT", "0")
    monkeypatch.delenv("NOEMA_EXECUTION_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("NOEMA_ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("NOEMA_LIVE_VENUES", raising=False)
    permitted, reasons = live_permission(AutonomyLevel.MICRO)
    assert permitted is False
    assert "execution gateway is disabled" in reasons


def test_halt_wins_over_earned_expansion(monkeypatch) -> None:
    _flags(monkeypatch, review="0", halt="1")
    permitted, reasons = live_permission(AutonomyLevel.EXPANSION)
    assert permitted is False
    assert reasons == ("owner master halt is active",)
    assert _authority(AutonomyLevel.EXPANSION) is None


def test_daily_cap_override_is_optional(monkeypatch) -> None:
    monkeypatch.delenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", raising=False)
    assert effective_daily_cap(AutonomyLevel.PROVEN, Decimal(1000)) == Decimal("300.00")
    monkeypatch.setenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "5")
    assert effective_daily_cap(AutonomyLevel.PROVEN, Decimal(1000)) == Decimal(5)
    monkeypatch.setenv("NOEMA_MAX_LIVE_DAILY_NOTIONAL_USD", "0")
    assert effective_daily_cap(AutonomyLevel.PROVEN, Decimal(1000)) == Decimal(0)
    _flags(monkeypatch, review="0", daily="0")
    assert _authority(AutonomyLevel.PROVEN) is None


def test_live_sizing_uses_level_fraction_instead_of_ten_dollar_cap(monkeypatch) -> None:
    _flags(monkeypatch, review="0")
    policy = risk_policy_for_level(RiskPolicy(), AutonomyLevel.PROVEN, Decimal(1000), live=True)
    action = RiskEngine(policy).decide(_opportunity(), bankroll_usd=1000)
    assert policy.mode is Mode.LIVE
    assert policy.max_stake_usd == 30
    assert action.decision is Decision.LIVE_BUY_YES
    assert action.stake_usd == 30

    paper = risk_policy_for_level(RiskPolicy(), AutonomyLevel.PAPER, Decimal(1000), live=False)
    assert paper.mode is Mode.PAPER
    assert paper.max_stake_usd == 10


def test_drawdown_demotes_immediately_and_removes_live_authority(monkeypatch) -> None:
    _flags(monkeypatch, review="0")
    earned = earned_autonomy(_proven_evidence(max_drawdown_fraction=Decimal("0.20")))
    transition = transition_autonomy(AutonomyLevel.PROVEN, earned.earned_level)
    assert transition.demoted is True
    assert transition.next_level is not AutonomyLevel.PROVEN
    assert not level_is_live(transition.next_level)


def level_is_live(level: AutonomyLevel) -> bool:
    return live_permission(level)[0]


class RecordingVenue:
    name = "kalshi:production"
    supports_live_execution = True
    supports_authoritative_reconciliation = True
    execution_economics_verified = True
    execution_account_state_current = True

    def __init__(self) -> None:
        self.calls = 0
        self.stakes: list[float] = []
        self.cancelled: list[str] = []

    async def execute(self, action, *, client_order_id):
        self.calls += 1
        self.stakes.append(action.stake_usd)
        return "order-1"

    async def cancel(self, order_id: str) -> str:
        self.cancelled.append(order_id)
        return order_id

    async def balances(self):
        return {"buying_power_usd": "1000"}

    async def reconcile_execution(self, _request):
        return None


def _proven_evidence(**overrides) -> AutonomyEvidence:
    values = {
        "resolved_forecasts": 1200,
        "live_observation_days": 70,
        "after_cost_return": Decimal("0.03"),
        "realized_net_pnl_usd": Decimal(100),
        "max_drawdown_fraction": Decimal("0.04"),
        "calibration_error": Decimal("0.04"),
        "reconciliation_ok_fraction": Decimal("0.999"),
        "profitable_days": 10,
        "losing_days": 4,
    }
    values.update(overrides)
    return AutonomyEvidence(**values)


def _micro_evidence() -> AutonomyEvidence:
    return AutonomyEvidence(
        resolved_forecasts=300,
        live_observation_days=30,
        after_cost_return=Decimal("0.01"),
        realized_net_pnl_usd=Decimal(10),
        max_drawdown_fraction=Decimal("0.04"),
        calibration_error=Decimal("0.06"),
        reconciliation_ok_fraction=Decimal("0.996"),
        profitable_days=5,
        losing_days=1,
    )


def _opportunity(market_id: str = "m1", when: datetime | None = None) -> Opportunity:
    moment = when or datetime.now(UTC)
    snapshot = MarketSnapshot(
        venue="kalshi:production", market_id=market_id, title="Test",
        yes_bid=0.50, yes_ask=0.51, no_bid=0.48, no_ask=0.49,
        liquidity_usd=5000, closes_at=None, resolution_rules="Objective binary resolution.",
        captured_at=moment,
    )
    forecast = Forecast(
        market_id=market_id, venue=snapshot.venue, probability_yes=0.90,
        lower_bound=0.88, upper_bound=0.92, model_version="test",
        evidence_ids=("evidence-1",), created_at=moment,
    )
    return Opportunity(
        forecast=forecast, snapshot=snapshot, market_probability=0.51,
        raw_edge=0.39, estimated_cost=0.01, uncertainty_penalty=0.02, robust_edge=0.36,
    )


@pytest.mark.asyncio
async def test_demotion_resizes_within_one_cycle_and_cancels_oversized_orders(
    tmp_path, monkeypatch,
) -> None:
    _flags(monkeypatch, review="0")
    venue = RecordingVenue()
    path = tmp_path / "authority.db"
    gateway = ExecutionGateway(str(path))
    coordinator = TradingCoordinator(
        str(path), gateway, RiskEngine(RiskPolicy()), venue,
    )
    coordinator.store_level(AutonomyLevel.PROVEN)
    try:
        now = datetime.now(UTC)
        first = await coordinator.run_cycle(
            [_opportunity(when=now - timedelta(seconds=2))],
            evidence=_proven_evidence(),
            bankroll_usd=5000,
        )
        assert first.level == "proven"
        assert first.submitted == 1
        assert venue.stakes == [30.0]
        second = await coordinator.run_cycle(
            [_opportunity(when=now)],
            evidence=_micro_evidence(),
            bankroll_usd=5000,
        )
    finally:
        coordinator.close()

    assert second.level == "micro"
    assert second.cancelled == 1
    assert venue.cancelled == ["order-1"]
    assert venue.stakes == [30.0, 10.0]
    assert second.submitted == 1
    import json
    import sqlite3
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT evidence_json FROM economic_events WHERE event_type='autonomy_transition'"
        ).fetchall()
    payload = json.loads(rows[-1][0])
    assert payload["demoted"] is True
    assert payload["next_level"] == "micro"
