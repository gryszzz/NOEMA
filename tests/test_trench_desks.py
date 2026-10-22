import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from noema.solana_live_gate import solana_desk_live_enabled
from noema.solana_research import PoolCandidate
from noema.trench_desk_runtime import DeskRuntimeConfig, run_desk_cycle
from noema.trench_desks import (
    HARD_STOP_CEILING,
    CostModel,
    DeskParams,
    Mark,
    PaperDeskEngine,
    TrenchDeskStore,
    desk_display_name,
    desk_overview,
    mutate_params,
)


def _engine(tmp_path, count=5, capital=500.0):
    store = TrenchDeskStore(str(tmp_path / "d.db"))
    store.ensure_desks(count, capital)
    return store, PaperDeskEngine(store, costs=CostModel(network_fee_usd=0.0))


def test_desks_created_with_bankroll_slices(tmp_path):
    store, _ = _engine(tmp_path)
    desks = store.active_desks()
    assert [d["name"] for d in desks] == ["VET", "RISK", "FILLS", "TAPE", "FLOOR"]
    assert all(d["cash_usd"] == 100.0 for d in desks)
    store.ensure_desks(5, 999)  # idempotent
    assert len(store.all_desks()) == 5


def test_entry_splits_core_and_bounce_with_costs(tmp_path):
    store, engine = _engine(tmp_path)
    assert engine.try_enter("vet-g1", "M", "WEFT", Mark(1.0, 1_000_000, "test"), "rules")
    pos = store.open_positions("vet-g1")[0]
    assert pos["core_cost_usd"] == pytest.approx(5.0)  # 10% of 50 allocation
    assert pos["bounce_cost_usd"] == pytest.approx(45.0)
    trades = list(store.conn.execute("SELECT * FROM trench_desk_trades"))
    assert all(t["paper"] == 1 and t["fee_usd"] > 0 and t["fill_price"] > 1.0 for t in trades)
    assert not engine.try_enter("vet-g1", "M", "WEFT", Mark(1.0, 1e6, "t"), "dup")


def test_take_profit_then_rebuy_on_dip(tmp_path):
    store, engine = _engine(tmp_path)
    engine.try_enter("vet-g1", "M", "W", Mark(1.0, 1e9, "t"), "r")
    engine.manage_positions("vet-g1", {"M": Mark(1.2, 1e9, "t")})
    desk = store.conn.execute("SELECT * FROM trench_desks WHERE desk_id='vet-g1'").fetchone()
    # partial bounce exit books P&L, but wins/losses are scored per closed position
    assert desk["wins"] == 0 and desk["realized_pnl_usd"] > 0
    pos = store.open_positions("vet-g1")[0]
    assert pos["bounce_qty"] == 0 and pos["core_qty"] > 0
    engine.manage_positions("vet-g1", {"M": Mark(1.1, 1e9, "t")})
    assert store.open_positions("vet-g1")[0]["bounce_qty"] > 0


def test_two_losses_fire_and_hire_mutated_successor(tmp_path):
    store, engine = _engine(tmp_path)
    for mint in ("A", "B"):
        engine.try_enter("vet-g1", mint, mint, Mark(1.0, 1e9, "t"), "r")
    engine.manage_positions("vet-g1", {"A": Mark(0.5, 1e9, "t"), "B": Mark(0.5, 1e9, "t")})
    successor = engine.fire_if_needed("vet-g1", {})
    assert successor == "vet-g2"
    fired = store.conn.execute("SELECT * FROM trench_desks WHERE desk_id='vet-g1'").fetchone()
    new = store.conn.execute("SELECT * FROM trench_desks WHERE desk_id='vet-g2'").fetchone()
    assert fired["status"] == "fired" and new["name"] == "VET II"
    assert new["parent_desk_id"] == "vet-g1"
    assert new["starting_bankroll_usd"] == pytest.approx(fired["cash_usd"])
    assert not store.open_positions("vet-g1")
    events = [r["event"] for r in store.conn.execute("SELECT event FROM trench_desk_events")]
    assert "desk_fired" in events
    engine.commit()
    overview = desk_overview(str(tmp_path / "d.db"))
    assert overview["paper"] and overview["fired"][0]["desk_id"] == "vet-g1"


def test_core_stop_and_max_hold(tmp_path):
    store, engine = _engine(tmp_path)
    engine.try_enter("risk-g1", "C", "C", Mark(1.0, 1e9, "t"), "r")
    engine.manage_positions("risk-g1", {"C": Mark(0.9, 1e9, "t")})
    assert not store.open_positions("risk-g1")
    engine.try_enter("risk-g1", "D", "D", Mark(1.0, 1e9, "t"), "r")
    later = datetime.now(UTC) + timedelta(days=2)
    engine.manage_positions("risk-g1", {"D": Mark(1.01, 1e9, "t")}, now=later)
    assert not store.open_positions("risk-g1")


def test_params_and_mutation_respect_hard_stop():
    with pytest.raises(ValueError):
        DeskParams(stop_loss_pct=0.08).validate()
    for seed in range(50):
        p = mutate_params(DeskParams(stop_loss_pct=0.079), str(seed))
        assert p.stop_loss_pct <= HARD_STOP_CEILING
    assert desk_display_name("VET", 2) == "VET II" and desk_display_name("VET", 4) == "VET IV"


def test_cost_model_slippage_scales_with_size():
    costs = CostModel()
    small = costs.slippage_fraction(10, 100_000)
    large = costs.slippage_fraction(10_000, 100_000)
    assert large > small
    price, fee, slip = costs.fill(side="sell", quote_price=1.0, notional_usd=100,
                                  liquidity_usd=1e6)
    assert price < 1.0 and fee > 0 and slip > 0


def test_runtime_stays_paper_until_solana_live_gate(monkeypatch):
    monkeypatch.setenv("NOEMA_ALLOW_LIVE_ORDERS", "1")
    assert solana_desk_live_enabled() is False


class _Gecko:
    async def token_top10_fraction(self, mint):
        return None

    async def trending_pools(self):
        created = datetime.now(UTC) - timedelta(hours=5)
        return [
            PoolCandidate("GOOD", "GOOD", "P1", created, 0.01, 80_000, 1e6, 1.0, 5.0, 10, 90, 9000, "g"),
            PoolCandidate("YOUNG", "YNG", "P2", datetime.now(UTC), 0.01, 80_000, 1e6, 1.0, 5.0, 1, 1, 1, "g"),
        ]

    async def new_pools(self):
        return []


class _Solana:
    async def top_account_supply_shares(self, mint):
        return (0.4, 0.05, 0.05)


class _Dex:
    async def tokens_by_mint(self, mints):
        return {m: {"priceUsd": "0.01", "liquidity": {"usd": 80_000}} for m in mints}


def test_full_cycle_records_paper_entries(tmp_path, monkeypatch):
    import noema.trench_desk_runtime as rt
    real_gate = rt.chief_gate
    # rules-only picks carry no confidence, so CHIEF vetoes them; approve here to exercise entries
    monkeypatch.setattr(rt, "chief_gate", lambda picks: (picks, real_gate(picks)[1]))
    for flag in ("NOEMA_EXECUTION_GATEWAY_ENABLED", "NOEMA_ALLOW_LIVE_ORDERS",
                 "NOEMA_TREASURY_ACTIONS_ENABLED", "NOEMA_SOLANA_SIGNER_ENABLED",
                 "NOEMA_DESK_JUDGEMENT_ENABLED"):
        monkeypatch.delenv(flag, raising=False)
    # This test exercises paper entries. Influence is covered separately and
    # would shadow every unproven agent.
    monkeypatch.setenv("NOEMA_ACCURACY_INFLUENCE_ENABLED", "0")
    db = str(tmp_path / "c.db")
    summary = asyncio.run(run_desk_cycle(
        db, config=DeskRuntimeConfig(enabled=True, paper_capital_usd=500.0),
        gecko=_Gecko(), solana=_Solana(), dex=_Dex()))
    assert summary["entry_passed"] == 1 and summary["entries"] == 5
    assert summary["rejections"]["pool younger than minimum age"] == 1
    assert summary["judgement"]["status"] == "rules_only"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM trench_desk_trades WHERE paper=1").fetchone()[0] == 10
    assert os.environ.get("NOEMA_ALLOW_LIVE_ORDERS") is None


def test_one_losing_two_leg_position_is_one_loss_and_does_not_fire(tmp_path):
    store, engine = _engine(tmp_path)
    engine.try_enter("vet-g1", "A", "A", Mark(1.0, 1e9, "t"), "r")
    engine.manage_positions("vet-g1", {"A": Mark(0.5, 1e9, "t")})
    assert not store.open_positions("vet-g1")
    desk = store.conn.execute("SELECT * FROM trench_desks WHERE desk_id='vet-g1'").fetchone()
    assert desk["losses"] == 1 and desk["consecutive_losses"] == 1
    assert engine.fire_if_needed("vet-g1", {}) is None
