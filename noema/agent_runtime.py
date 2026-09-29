from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime

import httpx
from polymarket_us.errors import PolymarketUSError

from .agent_config import AgentConfig
from .agent_identity import AgentIdentity
from .agent_models import AgentConnectionState, AgentCycleState, AgentStatus
from .agent_planner import choose_goal, refine_goal_with_cognition
from .agent_store import AgentStore
from .autonomous_research import run_research_work
from .baseline_recording import record_market_baseline
from .cognition import maybe_run_cognition
from .cognition_models import CognitionResult
from .config import kalshi_production_read_only_config
from .console_replication import publish_console_snapshot
from .cross_venue_experiment import mature_paper_pairs
from .economic_dashboard import build_economic_overview
from .ecosystem_controller import review_research_ecosystem
from .ecosystem_evolution import evolve_default_specialists
from .evm_watch import EvmWatchClient
from .history_forecaster import MODEL_VERSION, record_history_candidate
from .kalshi_telemetry import KalshiTelemetry
from .ledger import ForecastLedger
from .opportunity_radar import build_radar
from .outcomes import OutcomeStore
from .paper_research import PaperResearchStore, collect_paper_quote
from .provenance import EvidenceStore
from .research_allocation import CollectionQuotas, collection_quotas
from .soak import SoakStore
from .soak_runner import collect_rotating_market_batch
from .solana_research import JupiterTrenchResearchClient, SolanaRpcResearchClient
from .stripe_economy import sync_stripe_economy
from .sync import (
    cross_venue_outcome_targets,
    sync_kalshi_outcomes,
    sync_polymarket_us_outcomes,
)
from .trench_collector import collect_trench_cycle
from .trench_config import TrenchCollectorConfig
from .venues.kalshi import KalshiVenue
from .venues.kalshi_history import KalshiHistory
from .venues.polymarket_us import PolymarketUSVenue


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
        telemetry = KalshiTelemetry(kalshi_production_read_only_config())
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


async def _trench_state(
    db_path: str, *, quotas: CollectionQuotas | None = None,
) -> AgentConnectionState:
    config = TrenchCollectorConfig.from_env()
    if not config.enabled:
        return AgentConnectionState("disabled", "Trench collector disabled")
    try:
        config.validate()
        summary = await collect_trench_cycle(
            db_path=db_path,
            jupiter=JupiterTrenchResearchClient(api_key=config.jupiter_api_key),
            solana=SolanaRpcResearchClient(
                rpc_url=config.solana_rpc_url,
                fallback_rpc_url=config.solana_rpc_fallback_url,
            ),
            due_limit=config.due_limit,
            enrichment_limit=(config.enrichment_limit if quotas is None
                              else quotas.trench_enrichment),
            request_pause_seconds=config.request_pause_seconds,
            discover_new=quotas is None or quotas.trench_due > 0,
        )
        status = ("degraded" if summary.failed or summary.unavailable
                  or summary.provider_failures else "connected")
        return AgentConnectionState(
            status,
            (
                f"discovered={summary.discovered} due={summary.due} "
                f"recorded={summary.recorded} unavailable={summary.unavailable} "
                f"failed={summary.failed} assessments={summary.assessments_recorded} "
                f"counterfactuals={summary.counterfactuals_recorded} "
                f"provider_failures={','.join(summary.provider_failures) or 'none'}"
            ),
        )
    except httpx.HTTPStatusError as exc:
        host = exc.request.url.host or "configured provider"
        provider = "Jupiter" if host == "api.jup.ag" else "Solana RPC"
        return AgentConnectionState(
            "degraded",
            f"{provider} returned HTTP {exc.response.status_code} during read-only collection",
        )
    except httpx.RequestError as exc:
        host = exc.request.url.host or "configured provider"
        provider = "Jupiter" if host == "api.jup.ag" else "Solana RPC"
        return AgentConnectionState(
            "degraded",
            f"{provider} transport failure during read-only collection: {type(exc).__name__}",
        )
    except (
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        OSError,
    ) as exc:
        return AgentConnectionState(
            "degraded",
            f"{type(exc).__name__}: Trench collection failed",
        )


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
    try:
        stripe_economy = await sync_stripe_economy(config.db_path)
    except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
        stripe_economy = {"status": "unavailable", "reason": "read-only Stripe sync failed"}
    if stripe_economy.get("status") not in {"cached", "disabled"}:
        _log("agent_stripe_economy", status=stripe_economy.get("status"),
             payment_intent_count=stripe_economy.get("payment_intent_count"),
             succeeded_count=stripe_economy.get("succeeded_count"),
             new_successful_payment_count=stripe_economy.get("new_successful_payment_count"))

    # Persisted evidence determines real discretionary work before collecting data.
    # Small public baseline observation and existing outcome obligations continue even
    # when a strategy is quarantined; these are measurement, never trade authority.
    try:
        prior_plan = review_research_ecosystem(config.db_path)
        trench_config = TrenchCollectorConfig.from_env()
        trench_config.validate()
        quotas = collection_quotas(
            prior_plan, kalshi_market_limit=config.max_markets_per_cycle,
            kalshi_event_limit=config.max_event_checks_per_cycle,
            trench_due_limit=trench_config.due_limit,
            trench_enrichment_limit=trench_config.enrichment_limit, cycle_id=cycle_id,
        )
    except (sqlite3.Error, ValueError, OSError, KeyError, TypeError):
        quotas = CollectionQuotas()
        _log("agent_allocation_error", error="research allocation unavailable")

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
        venue = KalshiVenue(kalshi_production_read_only_config())
    except (httpx.HTTPError, RuntimeError, ValueError, OSError, TypeError) as exc:
        venue = None
        collection = None
        _log("agent_market_setup_error", error=type(exc).__name__)
    if venue is not None:
        try:
            collection = await collect_rotating_market_batch(
                venue,
                soak_store,
                max_markets=max(min(10, config.max_markets_per_cycle), quotas.kalshi_markets),
                on_valid=record_forecasts,
            )
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as exc:
            collection = None
            _log("agent_market_collection_error", error=type(exc).__name__)
        finally:
            await venue.close()

    # Polymarket US enters the same immutable forecast/evidence ledger as
    # Kalshi. Collection is public/read-only and never creates an order.
    polymarket_connection = AgentConnectionState("disabled", "public market collection disabled")
    if os.getenv("NOEMA_POLYMARKET_US_ENABLED", "1") == "1":
        try:
            polymarket = PolymarketUSVenue()
            try:
                pm_collection = await collect_rotating_market_batch(
                    polymarket,
                    soak_store,
                    max_markets=5,
                    on_valid=lambda market: record_market_baseline(market, forecast_ledger),
                )
                polymarket_connection = AgentConnectionState(
                    "connected" if pm_collection.valid else "degraded",
                    f"scanned={pm_collection.scanned} valid={pm_collection.valid} invalid={pm_collection.invalid}",
                )
            finally:
                polymarket.close()
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
            polymarket_connection = AgentConnectionState(
                "degraded", f"{type(exc).__name__}: public market collection failed",
            )

    # Verify complete event membership via the official public event endpoint.
    groups: dict[str, list] = {}
    for market in observed_markets:
        groups.setdefault(market.market_id.rsplit("-", 1)[0], []).append(market)
    if groups and quotas.kalshi_event_checks > 0:
        try:
            verifier = KalshiVenue(kalshi_production_read_only_config())
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
                    if checks_used >= quotas.kalshi_event_checks:
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
                            if hasattr(verifier, "paper_book") and hasattr(
                                verifier, "taker_fee_terms",
                            ):
                                try:
                                    result = await collect_paper_quote(
                                        verifier, PaperResearchStore(config.db_path),
                                        market.market_id,
                                    )
                                    _log("agent_paper_quote", status=result.status)
                                except (httpx.HTTPError, RuntimeError, ValueError,
                                        TypeError, KeyError) as exc:
                                    _log("agent_paper_quote_error", error=type(exc).__name__)
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

    kalshi, evm, trench = await asyncio.gather(
        _kalshi_state(),
        _evm_state(config),
        _trench_state(config.db_path, quotas=quotas),
    )
    radar = build_radar(config.db_path, limit=config.max_radar_rows)
    economic = build_economic_overview(config.db_path)
    economic_initialized = economic.get("snapshot") is not None

    try:
        evolution = evolve_default_specialists(config.db_path)
        ecosystem_plan = review_research_ecosystem(config.db_path)
        ecosystem_state = "active"
        ecosystem_focus = ecosystem_plan.dominant_specialist
        evolution_reviews = sum(
            int(item.review.reviewed)
            for item in (evolution.kalshi, evolution.trench)
        )
        challenger_count = sum(
            len(item.experiments)
            for item in (evolution.kalshi, evolution.trench)
        )
    except (sqlite3.Error, ValueError, OSError, KeyError) as exc:
        ecosystem_plan = None
        ecosystem_state = "degraded"
        ecosystem_focus = None
        evolution_reviews = 0
        challenger_count = 0
        _log("agent_ecosystem_error", error=type(exc).__name__)

    goal = choose_goal(
        radar=radar,
        market_data_healthy=market_data.status == "connected",
        ecosystem_focus=ecosystem_focus,
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
    try:
        research_work = await run_research_work(
            config.db_path, ecosystem_plan, selected_goal=goal.goal,
        )
    except (sqlite3.Error, ValueError, OSError, KeyError, TypeError):
        research_work = {"status": "degraded", "reason": "research policy or store unavailable"}
    if research_work.get("status") == "completed" and research_work.get("trial_id"):
        goal = replace(goal, goal="execute_registered_research",
                       reason=f"selected trial={research_work['trial_id']}")
        # The next wake uses this measured feedback; research results never promote
        # financial authority or rewrite immutable forecasting evidence.
        ecosystem_plan = review_research_ecosystem(config.db_path)
        ecosystem_focus = ecosystem_plan.dominant_specialist
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
        or ecosystem_state == "degraded"
        or trench.status == "degraded"
        or polymarket_connection.status == "degraded"
        or research_work["status"] in {"degraded", "failed", "timed_out"}
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
        ecosystem_state=ecosystem_state,
        ecosystem_focus=ecosystem_focus,
        cognition=cognition_state,
        market_data=market_data,
        trench=trench,
        polymarket_us=polymarket_connection,
        note=(
            f"{goal.reason}; research_work={research_work['status']}"
            if collection is None
            else (
                f"{goal.reason}; cognition={cognition_result.status}; "
                f"research_work={research_work['status']}; "
                f"research_reason={research_work['reason']}; "
                f"market_quota={quotas.kalshi_markets}; "
                f"event_quota={quotas.kalshi_event_checks}; "
                f"ecosystem_focus={ecosystem_focus or 'none'}; "
                f"ecosystem_idle={0.0 if ecosystem_plan is None else ecosystem_plan.idle_fraction:.3f}; "
                f"evolution_reviews={evolution_reviews}; "
                f"challengers_registered={challenger_count}; "
                f"trench={trench.status}; "
                f"polymarket_us={polymarket_connection.status}; "
                f"stripe_economy={stripe_economy.get('status')}; "
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
    store = OutcomeStore(config.db_path)
    kalshi_targets, polymarket_targets = cross_venue_outcome_targets(config.db_path)
    try:
        history = KalshiHistory(kalshi_production_read_only_config())
        try:
            result = await sync_kalshi_outcomes(
                store, history,
                max_markets=config.max_outcomes_per_sync,
                canonical_propositions=kalshi_targets,
            )
            _log("agent_outcome_sync", **asdict(result))
        except (httpx.HTTPError, RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
            _log("agent_outcome_sync_error", venue="kalshi", error=type(exc).__name__)
        finally:
            await history.close()
        if polymarket_targets:
            venue = PolymarketUSVenue()
            try:
                result = await sync_polymarket_us_outcomes(store, venue, polymarket_targets)
                _log("agent_polymarket_us_outcome_sync", **asdict(result))
            except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                    OSError, KeyError, TypeError, sqlite3.Error) as exc:
                _log("agent_polymarket_us_outcome_sync_error", error=type(exc).__name__)
            finally:
                venue.close()
    finally:
        store.conn.close()
    maturity = await asyncio.to_thread(mature_paper_pairs, config.db_path)
    _log("agent_cross_venue_maturation", **maturity)


async def run_agent(
    config: AgentConfig | None = None,
    *,
    identity: AgentIdentity | None = None,
) -> None:
    config = config or AgentConfig.from_env()
    config.validate()
    identity = identity or AgentIdentity()
    store = AgentStore(config.db_path)
    prior_status = store.read_status(identity)
    cycle_id = 0 if prior_status.last_cycle is None else prior_status.last_cycle.cycle_id

    starting = AgentStatus(
        agent_id=identity.agent_id,
        name=identity.name,
        version=identity.version,
        mission=identity.mission,
        running=True,
        last_heartbeat_at=datetime.now(UTC),
        last_cycle=prior_status.last_cycle,
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

    next_outcome_sync = 0.0
    try:
        while True:
            cycle_id += 1
            started = asyncio.get_running_loop().time()
            if started >= next_outcome_sync:
                next_outcome_sync = started + config.outcome_sync_interval_seconds
                try:
                    await _sync_outcomes(config)
                except (httpx.HTTPError, RuntimeError, ValueError,
                        OSError, KeyError, TypeError) as exc:
                    _log("agent_outcome_sync_error", error=type(exc).__name__)
            try:
                await run_cycle(
                    cycle_id=cycle_id, config=config, identity=identity, store=store,
                )
            except (httpx.HTTPError, sqlite3.Error, OSError, RuntimeError, ValueError,
                    KeyError, TypeError) as exc:
                _log("agent_cycle_error", error=type(exc).__name__, cycle_id=cycle_id)
            try:
                snapshot_status = await publish_console_snapshot(config.db_path)
            except (httpx.HTTPError, sqlite3.Error, OSError, RuntimeError, ValueError):
                snapshot_status = "unavailable"
            _log("agent_console_snapshot", status=snapshot_status, cycle_id=cycle_id)
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
