"""Shared fixtures.

Live-path tests predate the capital guard and exercise other gates, so the guard
is opened for them unless a test is marked ``capital_guard`` (which uses the real one).
"""
from __future__ import annotations

from decimal import Decimal

import pytest


def pytest_configure(config: pytest.Config) -> None:
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
