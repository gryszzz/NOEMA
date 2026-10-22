from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from noema import equity_feed as ef
from noema.capital_guard import CapitalGuard
from noema.room_v6 import capital_panel

pytestmark = pytest.mark.capital_guard

T0 = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)


def _src(name, usd, holdings=None, prices=None):
    async def read(client, prices_):
        return ef.SourceReading(name, Decimal(usd), {k: Decimal(v) for k, v in (holdings or {"USD": usd}).items()},
                                {k: Decimal(v) for k, v in (prices or {}).items()})
    return read


def _boom(client, prices_):
    async def inner():
        raise RuntimeError("rpc down")
    return inner()


def run(db, sources, now):
    return asyncio.run(ef.take_reading(str(db), sources=sources, client=_Dummy(), now=now))


class _Dummy:
    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("NOEMA_EQUITY_EXCLUDED_SOURCES", raising=False)
    monkeypatch.setenv("NOEMA_SOLANA_SIGNER_STATE_PATH", str(tmp_path / "sol.db"))
    monkeypatch.setenv("NOEMA_EVM_SIGNER_STATE_PATH", str(tmp_path / "evm.db"))


def test_complete_reading_feeds_guard_and_satisfies_freshness(tmp_path):
    db = tmp_path / "n.db"
    out = run(db, {"kalshi": _src("kalshi", "40"), "solana": _src("solana", "60")}, T0)
    assert out["status"] == "complete" and Decimal(out["total_usd"]) == 100
    guard = CapitalGuard(str(db))
    assert guard.state().floor == Decimal("75.00")
    assert "capital guard equity snapshot is stale" not in guard.block_reasons(now=T0 + timedelta(minutes=5))


def test_any_source_error_is_stale_not_partial(tmp_path):
    db = tmp_path / "n.db"
    run(db, {"kalshi": _src("kalshi", "100")}, T0)
    out = run(db, {"kalshi": _src("kalshi", "100"), "solana": _boom}, T0 + timedelta(minutes=5))
    assert out["status"] == "stale" and out["total_usd"] is None
    assert out["sources"]["solana"]["status"] == "error"
    guard = CapitalGuard(str(db))
    assert guard.state().updated_at == T0  # guard not updated with a partial total
    assert "capital guard equity snapshot is stale" in guard.block_reasons(now=T0 + timedelta(minutes=16))


def test_first_reading_error_leaves_no_baseline(tmp_path):
    db = tmp_path / "n.db"
    out = run(db, {"kalshi": _boom}, T0)
    assert out["status"] == "stale"
    assert CapitalGuard(str(db)).block_reasons(now=T0) == ["capital guard has no equity baseline"]


def test_excluded_source_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("NOEMA_EQUITY_EXCLUDED_SOURCES", "solana")
    db = tmp_path / "n.db"
    out = run(db, {"kalshi": _src("kalshi", "50"), "solana": _boom}, T0)
    assert out["status"] == "complete" and out["sources"]["solana"]["status"] == "excluded"
    assert Decimal(out["total_usd"]) == 50


def test_wallet_deposit_raises_floor_and_day_start(tmp_path):
    db = tmp_path / "n.db"
    run(db, {"solana": _src("solana", "100", {"SOL": "1"}, {"SOL": "100"})}, T0)
    out = run(db, {"solana": _src("solana", "300", {"SOL": "3"}, {"SOL": "100"})}, T0 + timedelta(minutes=5))
    assert Decimal(out["deposit_usd"]) == 200
    state = CapitalGuard(str(db)).state()
    assert state.peak_equity == 300 and state.floor == Decimal("225.00")
    assert state.day_start_equity == 300  # deposit is not counted as profit for the daily stop
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT source, asset, quantity FROM equity_feed_deposits").fetchall() == [
            ("solana", "SOL", "2")]


def test_price_move_is_not_a_deposit(tmp_path):
    db = tmp_path / "n.db"
    run(db, {"solana": _src("solana", "100", {"SOL": "1"}, {"SOL": "100"})}, T0)
    out = run(db, {"solana": _src("solana", "150", {"SOL": "1"}, {"SOL": "150"})}, T0 + timedelta(minutes=5))
    assert Decimal(out["deposit_usd"]) == 0
    assert CapitalGuard(str(db)).state().day_start_equity == 100


def test_jump_with_matching_trade_is_not_a_deposit(tmp_path):
    db = tmp_path / "n.db"
    run(db, {"kalshi": _src("kalshi", "100")}, T0)
    with sqlite3.connect(tmp_path / "sol.db") as conn:
        conn.execute("CREATE TABLE solana_swap_requests (updated_at TEXT)")
        conn.execute("INSERT INTO solana_swap_requests VALUES (?)", ((T0 + timedelta(minutes=2)).isoformat(),))
    out = run(db, {"kalshi": _src("kalshi", "180")}, T0 + timedelta(minutes=5))
    assert Decimal(out["deposit_usd"]) == 0


def test_small_jump_below_threshold_ignored():
    prev = {"kalshi": {"status": "ok", "equity_usd": "100", "holdings": {"USD": "100"}}}
    cur = {"kalshi": ef.SourceReading("kalshi", Decimal("100.5"), {"USD": Decimal("100.5")})}
    assert ef.detect_deposits(prev, cur, trades=0) == []


def test_capital_panel_reports_floor_and_daily_stop(tmp_path):
    db = tmp_path / "n.db"
    now = datetime.now(UTC)
    run(db, {"kalshi": _src("kalshi", "200")}, now)
    panel = capital_panel(str(db))
    assert Decimal(panel["guard"]["floor_usd"]) == 150
    assert Decimal(panel["guard"]["daily_stop_usd"]) == 180
    assert panel["guard"]["fresh"] is True
    assert panel["feed"]["sources"]["kalshi"]["status"] == "ok"


def test_feed_module_never_references_signer_keys():
    import inspect
    src = inspect.getsource(ef)
    for forbidden in ("PRIVATE_KEY", "wallet_signer", "SECRET_KEY", "keypair"):
        assert forbidden not in src


def test_wallet_deposit_and_withdrawal_flows() -> None:
    from decimal import Decimal as D
    prev = {"solana": {"status": "ok", "equity_usd": "0.33", "holdings": {"SOL": "0.00089088"}}}
    dep = {"solana": ef.SourceReading("solana", D("29.2"), {"SOL": D("0.28")}, {"SOL": D(104)})}
    got = ef.detect_deposits(prev, dep, trades=0)
    assert got and got[0][0] == "solana" and got[0][3] > 29
    prev2 = {"solana": {"status": "ok", "equity_usd": "29.2", "holdings": {"SOL": "0.30"}}}
    wd = {"solana": ef.SourceReading("solana", D("29.1"), {"SOL": D("0.28")}, {"SOL": D(104)})}
    out = ef.detect_deposits(prev2, wd, trades=0)
    assert out and out[0][3] < 0  # withdrawal booked as negative flow, not loss
    assert ef.detect_deposits(prev2, wd, trades=1) == []


def test_helius_rpc_preferred(monkeypatch) -> None:
    monkeypatch.setenv("HELIUS_API_KEY", "k")
    assert ef._solana_rpcs()[0].startswith("https://mainnet.helius-rpc.com/")
    monkeypatch.delenv("HELIUS_API_KEY")
    assert "helius" not in ef._solana_rpcs()[0]
