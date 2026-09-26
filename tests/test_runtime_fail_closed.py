import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from noema.agent_config import AgentConfig
from noema.agent_models import AgentConnectionState
from noema.agent_runtime import _evm_state, run_agent, run_cycle
from noema.agent_store import AgentStore
from noema.models import MarketSnapshot
from noema.outcomes import OutcomeStore
from noema.paired_evaluation import compare_history_to_market
from noema.paper_execution import FeeTerms
from noema.paper_research import PaperResearchStore


@pytest.mark.asyncio
async def test_bad_optional_wallet_setup_is_degraded_without_crashing() -> None:
    state = await _evm_state(AgentConfig(evm_rpc_url="https://example.com", evm_address="bad"))
    assert state.status == "degraded"
    assert "bad" not in state.detail


@pytest.mark.asyncio
async def test_rpc_error_does_not_expose_token_in_agent_status(monkeypatch) -> None:
    secret = "rpc-secret-do-not-print"
    async def fail():
        raise httpx.ConnectError(
            secret, request=httpx.Request("POST", f"https://example.com/{secret}")
        )
    monkeypatch.setattr("noema.agent_runtime.EvmWatchClient.snapshot", lambda self: fail())
    state = await _evm_state(AgentConfig(
        evm_rpc_url=f"https://example.com/{secret}", evm_address="0x" + "a" * 40,
    ))
    assert state.status == "degraded"
    assert secret not in state.detail


@pytest.mark.asyncio
async def test_market_adapter_setup_failure_is_recorded_without_secret(tmp_path, monkeypatch) -> None:
    secret = "private-path-secret"
    def fail():
        raise OSError(secret)
    monkeypatch.setattr("noema.agent_runtime.KalshiVenue", fail)
    monkeypatch.setattr(
        "noema.agent_runtime._kalshi_state",
        AsyncMock(return_value=AgentConnectionState("unconfigured")),
    )
    status = await run_cycle(
        cycle_id=1, config=AgentConfig(db_path=str(tmp_path / "noema.db")),
        runtime_running=False,
    )
    assert status.last_cycle is not None
    assert status.last_cycle.market_data.status == "degraded"
    assert status.last_cycle.active_goal == "restore_market_perception"
    assert secret not in status.last_cycle.note


@pytest.mark.asyncio
async def test_agent_survives_sync_failure_and_clears_running_flag(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "noema.db")
    called = asyncio.Event()

    async def fail_sync(config):
        raise RuntimeError("sensitive provider text")

    async def cycle(**kwargs):
        called.set()

    monkeypatch.setattr("noema.agent_runtime._sync_outcomes", fail_sync)
    monkeypatch.setattr("noema.agent_runtime.run_cycle", cycle)
    task = asyncio.create_task(run_agent(AgentConfig(db_path=db)))
    await asyncio.wait_for(called.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert AgentStore(db).read_status().running is False


@pytest.mark.asyncio
async def test_cycle_rejects_incomplete_event_then_records_verified_paper_pair(
    tmp_path, monkeypatch,
) -> None:
    db = str(tmp_path / "noema.db")
    now = datetime.now(UTC)
    store = OutcomeStore(db)
    for i in range(30):
        for side in ("A", "B"):
            store.upsert(
                venue="kalshi:demo", market_id=f"KXTEST-OLD{i}-{side}",
                outcome_yes=int(side == "A"),
                resolved_at=(now - timedelta(days=2)).isoformat(),
                seen_at=now - timedelta(days=1),
                raw={"event_ticker": f"KXTEST-OLD{i}"},
            )

    class FakeVenue:
        name = "kalshi:demo"
        complete = False

        async def market_page(self, *, cursor=None, limit=100):
            return [market async for market in self.markets()], None

        async def markets(self):
            for side in ("A", "B"):
                yield MarketSnapshot(
                    "kalshi:demo", f"KXTEST-NEW-{side}", "Clear question?",
                    .4, .5, .5, .6, 1000, None, "Clear resolution", captured_at=now,
                )

        async def event_market_tickers(self, event):
            assert event == "KXTEST-NEW"
            result = {"KXTEST-NEW-A", "KXTEST-NEW-B"}
            return result if self.complete else result | {"KXTEST-NEW-C"}

        async def taker_fee_terms(self, ticker):
            return FeeTerms("quadratic", Decimal(1))

        async def paper_book(self, ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.49", "2.00"]],
                                     "no_dollars": [["0.50", "2.00"]]}}, "public_top_of_book"

        async def close(self):
            pass

    monkeypatch.setattr("noema.agent_runtime.KalshiVenue", FakeVenue)
    monkeypatch.setattr(
        "noema.agent_runtime._kalshi_state",
        AsyncMock(return_value=AgentConnectionState("unconfigured")),
    )
    config = AgentConfig(db_path=db)
    rejected = await run_cycle(cycle_id=1, config=config, runtime_running=False)
    assert "history_candidates=0" in rejected.last_cycle.note
    FakeVenue.complete = True
    accepted = await run_cycle(cycle_id=2, config=config, runtime_running=False)
    assert "history_candidates=2" in accepted.last_cycle.note
    assert PaperResearchStore(db).audit()["observed_quote_count"] == 2
    assert compare_history_to_market(db).distinct_resolved_markets == 0
