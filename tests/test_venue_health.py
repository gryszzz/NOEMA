from datetime import UTC, datetime, timedelta

from noema.venue_health import assess_exchange_health


def test_healthy_exchange_state() -> None:
    now = datetime.now(UTC)
    result = assess_exchange_health(
        {"exchange_active": True, "trading_active": True},
        account_data_as_of=now,
        now=now,
    )
    assert result.healthy_for_decisions is True


def test_paused_exchange_is_unhealthy() -> None:
    now = datetime.now(UTC)
    result = assess_exchange_health(
        {"exchange_active": True, "trading_active": False},
        account_data_as_of=now,
        now=now,
    )
    assert result.healthy_for_decisions is False
    assert "trading inactive" in result.reasons


def test_stale_account_state_is_unhealthy() -> None:
    now = datetime.now(UTC)
    result = assess_exchange_health(
        {"exchange_active": True, "trading_active": True},
        account_data_as_of=now - timedelta(seconds=30),
        max_account_data_age_seconds=15,
        now=now,
    )
    assert result.healthy_for_decisions is False
    assert any("stale" in reason for reason in result.reasons)
