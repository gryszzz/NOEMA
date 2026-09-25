from datetime import UTC, datetime, timedelta

from noema.survival import CapitalGuardian, SurvivalState


def healthy_state() -> SurvivalState:
    now = datetime.now(UTC)
    return SurvivalState(
        starting_equity=1000,
        peak_equity=1050,
        current_equity=1020,
        daily_start_equity=1025,
        open_exposure=50,
        market_exposure=10,
        cash_balance=700,
        consecutive_losses=0,
        account_data_as_of=now,
    )


def test_healthy_state_allows_trading() -> None:
    decision = CapitalGuardian().assess(healthy_state())
    assert decision.trading_allowed is True
    assert decision.risk_multiplier == 1.0


def test_stale_account_data_halts() -> None:
    state = healthy_state()
    stale = SurvivalState(
        **{
            **state.__dict__,
            "account_data_as_of": datetime.now(UTC) - timedelta(minutes=1),
        }
    )
    decision = CapitalGuardian().assess(stale)
    assert decision.trading_allowed is False
    assert any("stale" in reason for reason in decision.reasons)


def test_drawdown_halts() -> None:
    state = healthy_state()
    bad = SurvivalState(**{**state.__dict__, "current_equity": 900})
    decision = CapitalGuardian().assess(bad)
    assert decision.trading_allowed is False
    assert "peak drawdown kill switch" in decision.reasons
