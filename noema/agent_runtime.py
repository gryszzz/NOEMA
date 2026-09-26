from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime

import httpx

from .agent_config import AgentConfig
from .agent_identity import AgentIdentity
from .agent_models import AgentConnectionState, AgentCycleState, AgentStatus
from .agent_planner import choose_goal, refine_goal_with_cognition
from .agent_store import AgentStore
from .baseline_recording import record_market_baseline
from .cognition import maybe_run_cognition
from .cognition_models import CognitionResult
from .economic_dashboard import build_economic_overview
from .evm_watch import EvmWatchClient
from .history_forecaster import MODEL_VERSION, record_history_candidate
from .kalshi_telemetry import KalshiTelemetry
from .ledger import ForecastLedger
from .opportunity_radar import build_radar
from .outcomes import OutcomeStore
from .provenance import EvidenceStore
from .soak import SoakStore
from .soak_runner import collect_market_snapshot_batch
from .sync import sync_kalshi_outcomes
from .venues.kalshi import KalshiVenue
from .venues.kalshi_history import KalshiHistory


def _log(event: str, **fields: object) -> None:
    print(
        json.dumps(
            {"ts": datetime.now(UTC).isoformat(), "event": event, **fields},
            sort_keys=True,
            default=str,
        ),
        flush=True,
    )


async def _kalshi_state() -> AgentConnectionState:
    try:
        telemetry = KalshiTelemetry()
    except (RuntimeError, ValueError, OSError, TypeError) as exc:
        return AgentConnectionState("unconfigured", f"{type(exc).__name__}: account unavailable")

    try:
        orders, fills, positions = await asyncio.gather(
            telemetry.orders(),
            telemetry.fills(),
            telemetry.positions(),
        )
        return AgentConnectionState(
            "connected",
            f"orders={len(orders)} fills={len(fills)} positions={len(positions)}",
        )
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
        return AgentConnectionState("degraded", f"{type(exc).__name__}: account check failed")
    finally:
        await telemetry.close()


async def _evm_state(config: AgentConfig) -> AgentConnectionState:
    if not config.evm_rpc_url or not config.evm_address:
        return AgentConnectionState("unconfigured", "dedicated EVM wallet not configured")

    try:
        client = EvmWatchClient(
            rpc_url=config.evm_rpc_url,
            address=config.evm_address,
        )
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as exc:
        return AgentConnectionState("degraded", f"{type(exc).__name__}: wallet setup failed")
    try:
        snapshot = await client.snapshot()
        return AgentConnectionState(
            "connected",
            (
                f"chain_id={snapshot.chain_id} block={snapshot.block_number} "
                f"native={snapshot.native_balance}"
            ),
        )
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
        return AgentConnectionState("degraded", f"{type(exc).__name__}: wallet RPC failed")
    finally:
        await client.close()


async def run_cycle(
    *,
    cycle_id: int,
    config: AgentConfig,
    identity: AgentIdentity | None = None,
    store: AgentStore | None = None,
    runtime_running: bool = True,
) -> AgentStatus:
    identity = identity or AgentIdentity()
    store = store or AgentStore(config.db_path)
    started = datetime.now(UTC)

    soak_store = SoakStore(config.db_path)
    forecast_ledger = ForecastLedger(config.db_path)
    outcome_store = OutcomeStore(config.db_path)
    evidence_store = EvidenceStore(config.db_path)
    candidates_recorded = 0
    observed_markets = []

    def record_forecasts(market):
        record_market_baseline(market, forecast_ledger)
        observed_markets.append(market)

    try:
        venue = KalshiVenue()
    except (httpx.HTTPError, RuntimeError, ValueError, OSError, TypeError) as exc:
        venue = None
        collection = None
        _log("agent_market_setup_error", error=type(exc).__name__)
    if venue is not None:
        try:
            collection = await collect_market_snapshot_batch(
                venue,
                soak_store,
                max_markets=config.max_markets_per_cycle,
                on_valid=record_forecasts,
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            collection = None
            _log("agent_market_collection_error", error=type(exc).__name__)
        finally:
            await venue.close()

    # Verify complete event membership via the official public event endpoint.
    groups: dict[str, list] = {}
    for market in observed_markets:
        groups.setdefault(market.market_id.rsplit("-", 1)[0], []).append(market)
    if groups:
        try:
            verifier = KalshiVenue()
        except (httpx.HTTPError, RuntimeError, ValueError, OSError, TypeError) as exc:
            verifier = None
            _log("agent_event_setup_error", error=type(exc).__name__)
        if verifier is not None:
            try:
                event_items = list(groups.items())
                start = (cycle_id * config.max_event_checks_per_cycle) % len(event_items)
                rotated = event_items[start:] + event_items[:start]
                checks_used = 0
                for event_ticker, group in rotated:
                    if len(group) not in {1, 2}:
                        continue
                    if all(forecast_ledger.has_model_forecast(
                        m.venue, m.market_id, MODEL_VERSION
                    ) for m in group):
                        continue
                    series = event_ticker.split("-", 1)[0]
                    prior_events = outcome_store.conn.execute(
                        """
                        SELECT COUNT(DISTINCT json_extract(raw_json, '$.event_ticker'))
                        FROM outcomes WHERE venue = ? AND market_id LIKE ?
                        """,
                        (group[0].venue, series + "-%"),
                    ).fetchone()[0]
                    if prior_events < 30:
                        continue
                    if checks_used >= config.max_event_checks_per_cycle:
                        break
                    checks_used += 1
                    try:
                        verified = await verifier.event_market_tickers(event_ticker)
                    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                        _log("agent_event_check_error", error=type(exc).__name__)
                        continue
                    if verified != {m.market_id for m in group}:
                        continue
                    for market in group:
                        if record_history_candidate(
                            market, outcomes=outcome_store, evidence=evidence_store,
                            ledger=forecast_ledger, current_event_size=len(verified),
                            verified_market_ids=frozenset(verified),
                        ):
                            candidates_recorded += 1
            finally:
                await verifier.close()

    if collection is None:
        market_data = AgentConnectionState("degraded", "market collection failed")
    elif collection.valid == 0:
        market_data = AgentConnectionState(
            "degraded", f"scanned={collection.scanned} valid=0"
        )
    else:
        market_data = AgentConnectionState(
            "connected", f"scanned={collection.scanned} valid={collection.valid}"
        )

    kalshi, evm = await asyncio.gather(
        _kalshi_state(),
        _evm_state(config),
    )
    radar = build_radar(config.db_path, limit=config.max_radar_rows)
    economic = build_economic_overview(config.db_path)
    economic_initialized = economic.get("snapshot") is not None

    goal = choose_goal(
        radar=radar,
        market_data_healthy=market_data.status == "connected",
    )

    if market_data.status == "connected":
        cognition_result = await maybe_run_cognition(
            radar,
            db_path=config.db_path,
        )
    else:
        cognition_result = CognitionResult(
            "skipped",
            detail="public market collection is not healthy",
        )

    goal = refine_goal_with_cognition(goal, cognition_result)
    cognition_state = AgentConnectionState(
        cognition_result.status,
        cognition_result.detail,
    )

    health = "healthy"
    if (
        market_data.status == "degraded"
        or kalshi.status == "degraded"
        or evm.status == "degraded"
        or cognition_result.status == "degraded"
    ):
        health = "degraded"
    elif kalshi.status == "unconfigured" or evm.status == "unconfigured":
        health = "partial"

    cycle = AgentCycleState(
        cycle_id=cycle_id,
        started_at=started,
        completed_at=datetime.now(UTC),
        active_goal=goal.goal,
        health=health,
        kalshi=kalshi,
        evm_wallet=evm,
        radar_markets=len(radar),
        economic_state="initialized" if economic_initialized else "uninitialized",
        cognition=cognition_state,
        market_data=market_data,
        note=(
            goal.reason
            if collection is None
            else (
                f"{goal.reason}; cognition={cognition_result.status}; "
                f"collected={collection.scanned} "
                f"valid={collection.valid} invalid={collection.invalid} "
                f"history_candidates={candidates_recorded}"
            )
        ),
    )
    status = AgentStatus(
        agent_id=identity.agent_id,
        name=identity.name,
        version=identity.version,
        mission=identity.mission,
        running=runtime_running,
        last_heartbeat_at=datetime.now(UTC),
        last_cycle=cycle,
    )
    store.write_status(status)
    store.append_heartbeat(status)
    _log("agent_cycle", **asdict(cycle))
    return status


async def _heartbeat_loop(
    store: AgentStore,
    identity: AgentIdentity,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        current = store.read_status(identity)
        if not current.running:
            continue
        heartbeat = replace(
            current,
            last_heartbeat_at=datetime.now(UTC),
        )
        store.write_status(heartbeat)
        store.append_heartbeat(heartbeat)


async def _sync_outcomes(config: AgentConfig) -> None:
    history = KalshiHistory()
    try:
        result = await sync_kalshi_outcomes(
            OutcomeStore(config.db_path), history,
            max_markets=config.max_outcomes_per_sync,
        )
        _log("agent_outcome_sync", **asdict(result))
    finally:
        await history.close()


async def run_agent(
    config: AgentConfig | None = None,
    *,
    identity: AgentIdentity | None = None,
) -> None:
    config = config or AgentConfig.from_env()
    config.validate()
    identity = identity or AgentIdentity()
    store = AgentStore(config.db_path)

    starting = AgentStatus(
        agent_id=identity.agent_id,
        name=identity.name,
        version=identity.version,
        mission=identity.mission,
        running=True,
        last_heartbeat_at=datetime.now(UTC),
        last_cycle=None,
    )
    store.write_status(starting)
    store.append_heartbeat(starting)
    _log("agent_started", agent_id=identity.agent_id, mission=identity.mission)

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            store,
            identity,
            config.heartbeat_interval_seconds,
        )
    )

    cycle_id = 0
    next_outcome_sync = 0.0
    try:
        while True:
            cycle_id += 1
            started = asyncio.get_running_loop().time()
            if started >= next_outcome_sync:
                next_outcome_sync = started + config.outcome_sync_interval_seconds
                try:
                    await _sync_outcomes(config)
                except (httpx.HTTPError, RuntimeError, ValueError, OSError, KeyError) as exc:
                    _log("agent_outcome_sync_error", error=type(exc).__name__)
            await run_cycle(
                cycle_id=cycle_id,
                config=config,
                identity=identity,
                store=store,
            )
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, config.cycle_interval_seconds - elapsed))
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        stopped = AgentStatus(
            agent_id=identity.agent_id,
            name=identity.name,
            version=identity.version,
            mission=identity.mission,
            running=False,
            last_heartbeat_at=datetime.now(UTC),
            last_cycle=store.read_status(identity).last_cycle,
        )
        store.write_status(stopped)
        store.append_heartbeat(stopped)
        _log("agent_stopped", agent_id=identity.agent_id)
