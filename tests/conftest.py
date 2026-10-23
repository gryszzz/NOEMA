"""Shared fixtures.

Live-path tests predate the capital guard and exercise other gates, so the guard
is opened for them unless a test is marked ``capital_guard`` (which uses the real one).
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("NOEMA_RPC_BUCKET", "0")  # rate limiting is tested directly
    config.addinivalue_line("markers", "capital_guard: run against the real capital guard")


@pytest.fixture(autouse=True)
def _open_capital_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("capital_guard"):
        return
    from noema import capital_guard

    monkeypatch.setattr(capital_guard.CapitalGuard, "block_reasons",
                        lambda self, *a, **k: [])
    monkeypatch.setattr(capital_guard.CapitalGuard, "max_trade_usd",
                        lambda self, *a, **k: Decimal("1e12"))


@pytest.fixture(autouse=True)
def _isolated_heartbeats(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the live runtime's heartbeat registry / skip log from tests."""
    d = tmp_path_factory.mktemp("hb")
    monkeypatch.setenv("NOEMA_HEARTBEAT_DIR", str(d / "beats"))
    monkeypatch.setenv("NOEMA_HEARTBEATS_JSON", str(d / "heartbeats.json"))
    from noema import heartbeats

    monkeypatch.setattr(heartbeats, "RUN_DIR", str(d / "run"))


@pytest.fixture(autouse=True)
def _isolated_risk_budget(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write the live risk_budget.json or read the live MQGB capacity file from tests."""
    d = tmp_path_factory.mktemp("rb")
    monkeypatch.setenv("NOEMA_RISK_BUDGET_PATH", str(d / "risk_budget.json"))
    monkeypatch.setenv("NOEMA_STRATEGY_CAPACITY_PATH", str(d / "strategy_capacity.json"))
    monkeypatch.setenv("NOEMA_PM_FEE_DB", str(d / "pm_fees.db"))
    from noema import pm_fees

    pm_fees._memo.clear()


@pytest.fixture(autouse=True)
def _isolated_suspends(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the box's real suspends.csv from tests (tests opt in with their own file)."""
    d = tmp_path_factory.mktemp("suspends")
    monkeypatch.setenv("NOEMA_SUSPENDS_CSV", str(d / "suspends.csv"))
