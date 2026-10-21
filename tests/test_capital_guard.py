from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from noema.capital_guard import (
    AgentSkill,
    CapitalGuard,
    effective_tier,
    proven_tier,
    size_prediction_stake,
)

pytestmark = pytest.mark.capital_guard
T0 = datetime(2026, 10, 21, 14, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("NOEMA_CAPITAL_FLOOR_FRACTION", "NOEMA_DAILY_LOSS_STOP_FRACTION",
                 "NOEMA_EQUITY_MAX_AGE_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_no_baseline_fails_closed(tmp_path):
    guard = CapitalGuard(str(tmp_path / "g.db"))
    assert guard.block_reasons() == ["capital guard has no equity baseline"]
    assert guard.max_trade_usd(tier="micro") == 0


def test_floor_ratchets_up_and_never_down(tmp_path):
    guard = CapitalGuard(str(tmp_path / "g.db"))
    assert guard.record_equity(100, now=T0).floor == Decimal("75.00")
    assert guard.record_equity(140, now=T0 + timedelta(minutes=1)).floor == Decimal("105.00")
    state = guard.record_equity(130, now=T0 + timedelta(days=1))
    assert state.floor == Decimal("105.00") and state.peak_equity == 140
    assert not state.tripped


def test_floor_breach_latches_until_owner_reset(tmp_path):
    guard = CapitalGuard(str(tmp_path / "g.db"))
    guard.record_equity(100, now=T0)
    guard.record_equity(120, now=T0 + timedelta(days=1))
    guard.record_equity(110, now=T0 + timedelta(days=2))
    tripped = guard.record_equity(89, now=T0 + timedelta(days=3))  # floor 90
    assert tripped.tripped and "floor" in tripped.trip_reason
    later = T0 + timedelta(days=3, minutes=1)
    assert guard.record_equity(200, now=later).tripped  # recovery does not auto-clear
    assert any("owner reset" in r for r in guard.block_reasons(now=later))
    with pytest.raises(PermissionError):
        guard.owner_reset(confirm="yes")
    assert not guard.owner_reset(confirm="I ACCEPT THE LOSS", now=later).tripped


def test_daily_loss_stop_at_ten_percent(tmp_path):
    guard = CapitalGuard(str(tmp_path / "g.db"))
    guard.record_equity(1000, now=T0)
    assert not guard.record_equity(910, now=T0 + timedelta(hours=1)).tripped
    state = guard.record_equity(899, now=T0 + timedelta(hours=2))
    assert state.tripped and "daily loss" in state.trip_reason


def test_deposit_raises_baseline_without_counting_as_gain(tmp_path):
    guard = CapitalGuard(str(tmp_path / "g.db"))
    guard.record_equity(100, now=T0)
    state = guard.record_equity(150, deposit_usd=50, now=T0 + timedelta(minutes=5))
    assert state.peak_equity == 150 and state.floor == Decimal("112.50")
    assert state.day_start_equity == 150 and not state.tripped


def test_stale_snapshot_blocks(tmp_path):
    guard = CapitalGuard(str(tmp_path / "g.db"))
    guard.record_equity(100, now=T0)
    assert "capital guard equity snapshot is stale" in guard.block_reasons(now=T0 + timedelta(hours=1))


def test_per_trade_cap_by_tier_and_headroom(tmp_path):
    guard = CapitalGuard(str(tmp_path / "g.db"))
    guard.record_equity(1000, now=T0)
    assert guard.max_trade_usd(tier="micro", now=T0) == Decimal("30.00")
    assert guard.max_trade_usd(tier="self_funded", now=T0) == Decimal("50.00")
    assert guard.max_trade_usd(tier="expansion", now=T0) == Decimal("80.00")
    assert guard.max_trade_usd(tier="paper", now=T0) == 0
    assert guard.block_reasons(31, tier="micro", now=T0) == [
        "trade exceeds the capital guard per-trade limit"]
    assert guard.block_reasons(30, tier="micro", now=T0) == []
    guard.record_equity(920, now=T0 + timedelta(minutes=1))  # 20 left of 10% daily budget
    assert guard.max_trade_usd(tier="expansion", now=T0 + timedelta(minutes=1)) == Decimal(20)


def test_clamps_reject_loosening(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_DAILY_LOSS_STOP_FRACTION", "0.9")
    monkeypatch.setenv("NOEMA_CAPITAL_FLOOR_FRACTION", "0.1")
    guard = CapitalGuard(str(tmp_path / "g.db"))
    state = guard.record_equity(100, now=T0)
    assert state.floor == Decimal("50.00")
    assert guard.record_equity(89, now=T0 + timedelta(minutes=1)).tripped


def test_accuracy_gates_tier():
    weak = AgentSkill(resolved=10, brier=0.30, market_brier=0.12, ev_after_cost=0.01)
    calibrated = AgentSkill(resolved=40, brier=0.20, market_brier=0.21, ev_after_cost=0.02)
    elite = AgentSkill(resolved=250, brier=0.11, market_brier=0.12, ev_after_cost=0.03)
    assert proven_tier(None) == "micro"
    assert proven_tier(weak) == "micro"
    assert proven_tier(calibrated) == "proven"
    assert proven_tier(elite) == "expansion"
    assert effective_tier("expansion", calibrated) == "proven"
    assert effective_tier("micro", elite) == "micro"
    assert effective_tier("paper", elite) == ""


def test_kelly_sizing_scales_with_confidence_and_calibration():
    equity, cap = Decimal(1000), Decimal(80)
    calibrated = AgentSkill(resolved=250, brier=0.11, market_brier=0.12, ev_after_cost=0.03)
    strong = size_prediction_stake(equity_cap_usd=cap, equity_usd=equity, tier="expansion",
                                   probability=0.80, price=0.50, skill=calibrated)
    small = size_prediction_stake(equity_cap_usd=cap, equity_usd=equity, tier="expansion",
                                  probability=0.53, price=0.50, skill=calibrated)
    unproven = size_prediction_stake(equity_cap_usd=cap, equity_usd=equity, tier="micro",
                                     probability=0.53, price=0.50, skill=None)
    assert strong == Decimal("80.00")  # top of tier
    assert small == Decimal("15.00")
    assert unproven == Decimal("7.50")
    assert size_prediction_stake(equity_cap_usd=cap, equity_usd=equity, tier="expansion",
                                 probability=0.40, price=0.50, skill=calibrated) == 0


def test_gateway_dex_swap_refused_without_guard(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from noema.execution_gateway import ExecutionGateway

    gateway = ExecutionGateway(str(tmp_path / "gw.db"))
    intent = SimpleNamespace(venue="jupiter", mission_id="m", evidence_ids=("e",), notional_usd=5)
    assert "capital guard has no equity baseline" in gateway._dex_swap_block_reasons(intent)
