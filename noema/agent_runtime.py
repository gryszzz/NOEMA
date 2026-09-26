from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime

from .agent_config import AgentConfig
from .agent_identity import AgentIdentity
from .agent_models import AgentConnectionState, AgentCycleState, AgentStatus
from .agent_planner import choose_goal
from .agent_store import AgentStore
from .economic_dashboard import build_economic_overview
from .evm_watch import EvmWatchClient
from .kalshi_telemetry import KalshiTelemetry
from .opportunity_radar import build_radar
from .soak import SoakStore
from .soak_runner import collect_market_snapshot_batch
from .venues.kalshi import KalshiVenue


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
    except Exception as exc:
        return AgentConnectionState("unconfigured", str(exc))

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
    except Exception as exc:
        return AgentConnectionState("degraded", str(exc))
    finally:
        await telemetry.close()


async def _evm_state(config: AgentConfig) -> AgentConnectionState:
    if not config.evm_rpc_url or not config.evm_address:
        return AgentConnectionState("unconfigured", "dedicated EVM wallet not configured")

    client = EvmWatchClient(
        rpc_url=config.evm_rpc_url,
        address=config.evm_address,
    )
    try:
        snapshot = await client.snapshot()
        return AgentConnectionState(
            "connected",
            (
                f"chain_id={snapshot.chain_id} block={snapshot.block_number} "
                f"native={snapshot.native_balance}"
            ),
        )
    except Exception as exc:
        return AgentConnectionState("degraded", str(exc))
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
    venue = KalshiVenue()
    try:
        collection = await collect_market_snapshot_batch(
            venue,
            soak_store,
            max_markets=config.max_markets_per_cycle,
        )
    except Exception as exc:
        collection = None
        _log("agent_market_collection_error", error=type(exc).__name__)
    finally:
        await venue.close()

    kalshi, evm = await asyncio.gather(
        _kalshi_state(),
        _evm_state(config),
    )
    radar = build_radar(config.db_path, limit=config.max_radar_rows)
    economic = build_economic_overview(config.db_path)
    economic_initialized = economic.get("snapshot") is not None

    goal = choose_goal(
        radar=radar,
        kalshi_healthy=kalshi.status == "connected",
        wallet_healthy=evm.status == "connected",
        economic_initialized=economic_initialized,
    )

    health = "healthy"
    if kalshi.status == "degraded" or evm.status == "degraded":
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
        note=(
            goal.reason
            if collection is None
            else (
                f"{goal.reason}; collected={collection.scanned} "
                f"valid={collection.valid} invalid={collection.invalid}"
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
    try:
        while True:
            cycle_id += 1
            started = asyncio.get_running_loop().time()
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
