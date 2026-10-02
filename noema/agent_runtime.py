from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from time import perf_counter

import httpx
from polymarket_us.errors import PolymarketUSError

from .agent_config import AgentConfig
from .agent_identity import AgentIdentity
from .agent_models import AgentConnectionState, AgentCycleState, AgentStatus
from .agent_planner import choose_goal, refine_goal_with_cognition
from .agent_store import AgentStore
from .autonomous_research import run_research_work
from .baseline_recording import record_market_baseline
from .bill_tracker import BillTracker
from .chain_registry import load_evm_chains
from .cognition import maybe_run_cognition
from .cognition_models import CognitionResult
from .config import kalshi_production_read_only_config
from .console_replication import publish_console_snapshot
from .cross_venue_experiment import mature_paper_pairs
from .diagnostics import kalshi_runtime_credential_diagnostic
from .economic_dashboard import build_economic_overview
from .economic_investigations import advance_current_period_investigation
from .economic_ledger import EconomicLedger
from .ecosystem_controller import review_research_ecosystem
from .ecosystem_evolution import evolve_default_specialists
from .history_forecaster import MODEL_VERSION, record_history_candidate
from .kalshi_telemetry import KalshiTelemetry
from .ledger import ForecastLedger
from .market_discovery import (
    MarketDiscoveryConfig,
    MarketDiscoveryError,
    collect_and_persist_market_discovery,
)
from .opportunity_radar import build_radar
from .outcomes import OutcomeStore
from .paper_research import PaperResearchStore, collect_paper_quote
from .polymarket_account_stream import run_polymarket_account_stream
from .prediction_venues import sample_polymarket_private_account
from .provenance import EvidenceStore
from .research_allocation import CollectionQuotas, collection_quotas
from .runtime_diagnostics import capture_process_identity
from .soak import SoakStore
from .soak_runner import collect_rotating_market_batch
from .solana_research import (
    DexScreenerTrenchPriceClient,
    JupiterTrenchResearchClient,
    SolanaRpcResearchClient,
)
from .sqlite_diagnostics import sqlite_error_fields
from .stripe_economy import reconcile_persisted_stripe_evidence, sync_stripe_economy
from .sync import (
    cross_venue_outcome_targets,
    sync_kalshi_outcomes,
    sync_polymarket_us_outcomes,
)
from .trench_collector import collect_trench_cycle
from .trench_config import TrenchCollectorConfig
from .venues.kalshi import KalshiCredentialError, KalshiVenue
from .venues.kalshi_history import KalshiHistory
from .venues.polymarket_us import PolymarketUSVenue
from .wallet_credentials import polymarket_us_credentials_present
from .wallet_observer import PublicWalletObserver


def _log(event: str, **fields: object) -> None:
    print(
        json.dumps(
            {"ts": datetime.now(UTC).isoformat(), "event": event, **fields},
            sort_keys=True,
            default=str,
        ),
        flush=True,
    )


def _polymarket_account_sample_interval() -> int:
    try:
        return max(60, min(3600, int(os.getenv(
            "NOEMA_POLYMARKET_ACCOUNT_SAMPLE_INTERVAL_SECONDS", "300",
        ))))
    except ValueError:
        return 300


async def _polymarket_account_sampler(
    db_path: str, on_change: Callable[[], None] | None = None,
) -> None:
    """Sample through the existing authenticated read-only path on the worker DB."""
    while True:
        try:
            result = await sample_polymarket_private_account(db_path)
            if result.get("records_persisted", 0) and on_change is not None:
                on_change()
            presence = result.get("credential_presence", {})
            stream = result.get("private_stream", {})
            _log(
                "polymarket_private_rest_sample",
                credential_key_id_present=presence.get("key_id", False),
                credential_secret_present=presence.get("secret_key", False),
                status=result.get("status", "unavailable"),
                failure_class=result.get("failure_class"),
                private_stream=stream.get("state", "unknown"),
                records_persisted=result.get("records_persisted", 0),
                sqlite_error_code=result.get("sqlite_error_code"),
                sqlite_error_name=result.get("sqlite_error_name"),
            )
        except Exception as exc:  # noqa: BLE001 - keep the scheduled read-only sample alive
            key_id_present, secret_present = polymarket_us_credentials_present()
            _log(
                "polymarket_private_rest_sample",
                credential_key_id_present=key_id_present,
                credential_secret_present=secret_present,
                status="failure", failure_class=type(exc).__name__,
                records_persisted=0,
                **sqlite_error_fields(exc),
            )
        await asyncio.sleep(_polymarket_account_sample_interval())


async def _changed_console_snapshot_loop(
    db_path: str,
    changed: asyncio.Event,
    publish_lock: asyncio.Lock,
    publish: Callable[[str], Awaitable[dict[str, object]]] = publish_console_snapshot,
    *,
    debounce_seconds: float = 0.2,
    min_interval_seconds: float = 15.0,
) -> None:
    """Refresh the console replica soon after real collector writes, with coalescing."""
    loop = asyncio.get_running_loop()
    last_started = float("-inf")
    while True:
        await changed.wait()
        changed.clear()
        await asyncio.sleep(debounce_seconds)
        changed.clear()
        delay = min_interval_seconds - (loop.time() - last_started)
        if delay > 0:
            await asyncio.sleep(delay)
            changed.clear()
        async with publish_lock:
            # A periodic snapshot may have held the lock while newer collector
            # writes arrived. This backup captures their current DB state too.
            changed.clear()
            last_started = loop.time()
            try:
                status = await publish(db_path)
            except Exception as exc:  # noqa: BLE001 - keep live collectors independent
                status = {
                    "status": "unavailable",
                    "failure_stage": "change_triggered_snapshot",
                    "failure_classification": "snapshot_publish_failure",
                    "error_type": type(exc).__name__,
                }
        _log("agent_console_snapshot", trigger="collector_change", **status)


def bootstrap_hosted_bill_budget(db_path: str) -> str | None:
    """Apply owner-supplied bootstrap values only on Render and only if absent."""
    if os.getenv("RENDER", "").lower() != "true":
        return None
    tracker = BillTracker(db_path)
    try:
        return tracker.bootstrap_hosted_budget_from_env()
    finally:
        tracker.conn.close()


async def _kalshi_state() -> AgentConnectionState:
    config = kalshi_production_read_only_config()
    _log("agent_kalshi_credential_diagnostic",
         **kalshi_runtime_credential_diagnostic(config))
    try:
        telemetry = KalshiTelemetry(config)
    except KalshiCredentialError as exc:
        detail_by_code = {
            "api_key_id_invalid_header_value": "API key ID has invalid request-header formatting",
            "private_key_unavailable": "private key unavailable",
            "private_key_source_unavailable": "private key source unreadable",
            "private_key_malformed": "private key malformed or incompatible",
            "private_key_incompatible": "private key type incompatible with Kalshi signing",
            "private_key_signing_failed": "private key signing failed",
        }
        detail = detail_by_code.get(exc.code, "credential initialization failed")
        _log("agent_kalshi_account_check", result="credential_failure", failure=exc.code)
        return AgentConnectionState("degraded", detail)
    except (RuntimeError, ValueError, OSError, TypeError) as exc:
        # Never include the exception message: constructors may contain secret-derived text.
        error_class = type(exc).__name__
        _log("agent_kalshi_account_check", result="initialization_failure", error_class=error_class)
        return AgentConnectionState("degraded", f"local credential initialization failed ({error_class})")

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
    except KalshiCredentialError as exc:
        detail = "private key signing failed" if exc.code == "private_key_signing_failed" else "private key incompatible"
        _log("agent_kalshi_account_check", result="credential_failure", failure=exc.code)
        return AgentConnectionState("degraded", detail)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in {401, 403}:
            result, detail = "authenticated_api_rejection", f"authenticated API rejected credentials (HTTP {code})"
        elif code == 429 or code >= 500:
            result, detail = "api_unavailable", f"Kalshi API unavailable (HTTP {code})"
        else:
            result, detail = "api_rejection", f"Kalshi API rejected account request (HTTP {code})"
        _log("agent_kalshi_account_check", result=result, http_status=code)
        return AgentConnectionState("degraded", detail)
    except httpx.LocalProtocolError as exc:
        error_class = type(exc).__name__
        _log("agent_kalshi_account_check", result="request_protocol_failure", error_class=error_class)
        return AgentConnectionState("degraded", "Kalshi request could not be formed safely")
    except httpx.RequestError as exc:
        # URLs, request headers, and exception strings are intentionally omitted.
        error_class = type(exc).__name__
        _log("agent_kalshi_account_check", result="network_failure", error_class=error_class)
        return AgentConnectionState("degraded", f"Kalshi network request failed ({error_class})")
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        error_class = type(exc).__name__
        result = "api_response_failure" if isinstance(exc, (ValueError, KeyError, TypeError)) else "api_request_failure"
        _log("agent_kalshi_account_check", result=result, error_class=error_class)
        return AgentConnectionState("degraded", f"Kalshi account request failed ({error_class})")
    finally:
        await telemetry.close()


async def _evm_state(config: AgentConfig) -> AgentConnectionState:
    address = (config.evm_address or os.getenv("NOEMA_EVM_ADDRESS") or "").strip()
    if not address:
        return AgentConnectionState("unconfigured", "public EVM wallet address not configured")
    try:
        chains = list(load_evm_chains())
        if config.evm_rpc_url:
            chains = [
                replace(chain, rpc_endpoint=config.evm_rpc_url,
                        rpc_provider="configured_json_rpc") if chain.chain_id == 1 else chain
                for chain in chains
            ]
        configured = [chain for chain in chains if chain.rpc_endpoint]
        if not configured:
            return AgentConnectionState("unconfigured", "EVM chain registry has no RPC endpoints")
        async with PublicWalletObserver() as observer:
            results = await asyncio.gather(*(observer.read_evm(chain, address) for chain in configured))
        connected = sum(item.get("status") == "read_only_balance" for item in results)
        failed = len(results) - connected
        detail = f"read_only_chains={connected}/{len(results)} rpc_failures={failed}"
        invalid = sum(item.get("status") == "invalid_address" for item in results)
        status = "connected" if connected else "degraded"
        if invalid:
            status = "degraded"
            detail = f"public address invalid; read_only_chains=0/{len(results)} rpc_failures={failed}"
        return AgentConnectionState(status, detail)
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as exc:
        return AgentConnectionState("degraded", f"{type(exc).__name__}: wallet observation failed")


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
            price_fallback=DexScreenerTrenchPriceClient(),
            due_limit=config.due_limit,
            enrichment_limit=(config.enrichment_limit if quotas is None
                              else quotas.trench_enrichment),
            request_pause_seconds=config.request_pause_seconds,
            discover_new=quotas is None or quotas.trench_due > 0,
        )
        required_provider_failures = tuple(
            failure for failure in summary.provider_failures
            if not failure.startswith("solana_rpc:")
        )
        status = ("degraded" if summary.failed or summary.unavailable
                  or required_provider_failures else "connected")
        return AgentConnectionState(
            status,
            (
                f"discovered={summary.discovered} due={summary.due} "
                f"recorded={summary.recorded} unavailable={summary.unavailable} "
                f"failed={summary.failed} assessments={summary.assessments_recorded} "
                f"counterfactuals={summary.counterfactuals_recorded} "
                f"cycle_provider_failures={','.join(summary.provider_failures) or 'none'} "
                f"provider_health={','.join(summary.provider_health) or 'unknown'}"
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
        if isinstance(exc, sqlite3.Error):
            _log("agent_trench_collection_error", error=type(exc).__name__,
                 **sqlite_error_fields(exc))
        return AgentConnectionState(
            "degraded",
            f"{type(exc).__name__}: Trench collection failed",
        )


async def _trench_sampler_loop(
    db_path: str,
    config: TrenchCollectorConfig,
    on_change: Callable[[], None] | None = None,
) -> None:
    """Collect scheduled launch snapshots independently of the long reasoning cycle."""
    config.validate()
    jupiter = JupiterTrenchResearchClient(api_key=config.jupiter_api_key)
    solana = SolanaRpcResearchClient(
        rpc_url=config.solana_rpc_url,
        fallback_rpc_url=config.solana_rpc_fallback_url,
    )
    fallback = DexScreenerTrenchPriceClient()
    interval = config.sample_interval_seconds
    discovery_interval = config.discovery_interval_seconds
    next_discovery = 0.0
    while True:
        started = asyncio.get_running_loop().time()
        discover = started >= next_discovery
        if discover:
            next_discovery = started + discovery_interval
        try:
            summary = await collect_trench_cycle(
                db_path=db_path,
                jupiter=jupiter,
                solana=solana,
                price_fallback=fallback,
                due_limit=config.due_limit,
                enrichment_limit=0,
                request_pause_seconds=0,
                discover_new=discover,
            )
            if on_change is not None and any((
                summary.discovered, summary.recorded,
                summary.assessments_recorded, summary.counterfactuals_recorded,
            )):
                on_change()
            if summary.due:
                _log(
                    "trench_forward_sampler",
                    due=summary.due,
                    recorded=summary.recorded,
                    unavailable=summary.unavailable,
                    failed=summary.failed,
                    assessments_recorded=summary.assessments_recorded,
                    counterfactuals_recorded=summary.counterfactuals_recorded,
                )
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, sqlite3.Error, OSError, RuntimeError, ValueError,
                KeyError, TypeError) as exc:
            _log("trench_forward_sampler_error", error=type(exc).__name__,
                 **sqlite_error_fields(exc))
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(0.0, interval - elapsed))


async def _market_discovery_sampler_loop(
    db_path: str,
    config: MarketDiscoveryConfig,
    on_change: Callable[[], None] | None = None,
) -> None:
    """Collect a small read-only DEX discovery batch on a bounded cadence."""
    config.validate()
    while True:
        started = asyncio.get_running_loop().time()
        try:
            result = await collect_and_persist_market_discovery(
                db_path,
                token_limit=config.token_limit,
                pair_limit_per_token=config.pair_limit_per_token,
            )
            if result.get("observations_persisted", 0) and on_change is not None:
                on_change()
            _log(
                "market_pair_discovery",
                status=result.get("status", "unavailable"),
                profiles_seen=result.get("profiles_seen", 0),
                tokens_queried=result.get("tokens_queried", 0),
                pairs_observed=result.get("pairs_observed", 0),
                pairs_rejected=result.get("pairs_rejected", 0),
                observations_persisted=result.get("observations_persisted", 0),
                duplicates=result.get("duplicates", 0),
                failed_requests=result.get("failed_requests", 0),
            )
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, sqlite3.Error, OSError, RuntimeError, ValueError,
                KeyError, TypeError) as exc:
            diagnostics: dict[str, object] = {"error": type(exc).__name__}
            if isinstance(exc, MarketDiscoveryError):
                diagnostics.update(
                    operation=exc.operation,
                    failure_class=exc.error_class,
                    http_status=exc.http_status,
                )
            _log(
                "market_pair_discovery_error",
                **diagnostics,
                **sqlite_error_fields(exc),
            )
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(0.0, config.interval_seconds - elapsed))


def _cycle_health(
    *,
    market_data: AgentConnectionState,
    ecosystem_state: str,
    ecosystem_focus: str | None,
    trench: AgentConnectionState,
    active_goal: str,
    cognition: AgentConnectionState,
    research_status: str,
) -> str:
    """Classify health from core services and the capability selected this cycle.

    Wallet/account telemetry, optional cognition, and unselected research providers
    remain visible in their own connection states without degrading core liveness.
    """
    required = {
        "market_data": market_data.status,
        "ecosystem": ecosystem_state,
    }
    if active_goal == "execute_registered_research" and ecosystem_focus == "trench-1":
        required["trench"] = trench.status
    if active_goal in {"model_guided_investigation", "collect_requested_research"}:
        required["cognition"] = cognition.status
    if active_goal == "execute_registered_research":
        required["research"] = research_status

    if any(value in {"degraded", "failed", "timed_out", "unavailable"}
           for value in required.values()):
        return "degraded"
    if any(value in {"unconfigured", "disabled", "unknown"}
           for value in required.values()):
        return "partial"
    return "healthy"


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
    cycle_clock = perf_counter()
    stage_started = cycle_clock
    stage_timings: dict[str, float] = {}

    def finish_stage(name: str) -> None:
        nonlocal stage_started
        now_clock = perf_counter()
        stage_timings[name] = round(max(0.0, now_clock - stage_started), 6)
        stage_started = now_clock

    stage_name = "economic_manifest_and_stripe_reconciliation"
    try:
        ledger = EconomicLedger(config.db_path)
        try:
            ledger.ensure_current_period_manifest(provenance={
                "source": "NOEMA autonomous runtime", "cycle_id": cycle_id,
                "manifest_version": "core-economic-sources-v1",
            })
        finally:
            ledger.conn.close()
    except (sqlite3.Error, OSError, ValueError):
        _log("economic_period_manifest_error", status="unavailable")
    try:
        stripe_economy = await sync_stripe_economy(config.db_path)
        stripe_canonical = reconcile_persisted_stripe_evidence(config.db_path)
        _log("agent_stripe_canonical_reconciliation", **stripe_canonical)
    except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError):
        stripe_economy = {"status": "unavailable", "reason": "read-only Stripe sync failed"}
        stripe_canonical = {"status": "unavailable"}
    if stripe_economy.get("status") not in {"cached", "disabled"}:
        _log("agent_stripe_economy", status=stripe_economy.get("status"),
             credential_present=bool(os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_API_KEY")),
             payment_intent_count=stripe_economy.get("payment_intent_count"),
             record_count=stripe_economy.get("persisted_record_count",
                                             stripe_economy.get("record_count")),
             succeeded_count=stripe_economy.get("succeeded_count"),
             new_successful_payment_count=stripe_economy.get("new_successful_payment_count"))
    finish_stage(stage_name)
    stage_name = "research_allocation"

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
    except (sqlite3.Error, ValueError, OSError, KeyError, TypeError) as exc:
        quotas = CollectionQuotas()
        _log("agent_allocation_error", error="research allocation unavailable",
             **sqlite_error_fields(exc))
    finish_stage(stage_name)
    stage_name = "kalshi_market_collection"

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
    finish_stage(stage_name)
    stage_name = "polymarket_market_collection"

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
    finish_stage(stage_name)
    stage_name = "event_verification_and_forecast_candidates"

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
    finish_stage(stage_name)
    stage_name = "account_and_trench_health"

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
    finish_stage(stage_name)
    stage_name = "specialist_evolution"

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
            sum(experiment.status == "registered" for experiment in item.experiments)
            for item in (evolution.kalshi, evolution.trench)
        )
        deferred_challenger_count = sum(
            sum(experiment.status == "deferred" for experiment in item.experiments)
            for item in (evolution.kalshi, evolution.trench)
        )
        admitted_challenger_count = len(evolution.admitted_trial_ids)
    except (sqlite3.Error, ValueError, OSError, KeyError) as exc:
        ecosystem_plan = None
        ecosystem_state = "degraded"
        ecosystem_focus = None
        evolution_reviews = 0
        challenger_count = 0
        deferred_challenger_count = 0
        admitted_challenger_count = 0
        _log("agent_ecosystem_error", error=type(exc).__name__)
    finish_stage(stage_name)
    stage_name = "cognition_and_goal_selection"

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
    finish_stage(stage_name)
    stage_name = "research_work"
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
    finish_stage(stage_name)
    stage_name = "economic_investigation"
    cognition_state = AgentConnectionState(
        cognition_result.status,
        cognition_result.detail,
    )

    health = _cycle_health(
        market_data=market_data,
        ecosystem_state=ecosystem_state,
        ecosystem_focus=ecosystem_focus,
        trench=trench,
        active_goal=goal.goal,
        cognition=cognition_state,
        research_status=research_work["status"],
    )

    try:
        investigation_decision = advance_current_period_investigation(
            config.db_path, now=datetime.now(UTC),
        )
        _log("agent_economic_investigation_decision",
             action=investigation_decision["action"],
             selected_candidate=investigation_decision["selected_candidate"],
             decision_id=investigation_decision["decision_id"],
             created_now=investigation_decision["created_now"],
             owner_input_required=bool(investigation_decision["owner_input_required"]))
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        investigation_decision = None
        _log("agent_economic_investigation_unavailable", status="degraded")
    finish_stage(stage_name)

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
        duration_seconds=round(perf_counter() - cycle_clock, 6),
        cadence_seconds=config.cycle_interval_seconds,
        stage_timings=dict(stage_timings),
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
                f"challengers_deferred={deferred_challenger_count}; "
                f"challengers_admitted={admitted_challenger_count}; "
                f"trench={trench.status}; "
                f"polymarket_us={polymarket_connection.status}; "
                f"stripe_economy={stripe_economy.get('status')}; "
                f"collected={collection.scanned} "
                f"valid={collection.valid} invalid={collection.invalid} "
                f"history_candidates={candidates_recorded}; "
                f"accounting_decision={('unavailable' if investigation_decision is None else investigation_decision['action'] + ':' + str(investigation_decision['selected_candidate']))}"
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
    persistence_started = perf_counter()
    store.write_status(status)
    store.append_heartbeat(status)
    stage_timings["state_persistence"] = round(perf_counter() - persistence_started, 6)
    total_duration = round(perf_counter() - cycle_clock, 6)
    store.append_cycle_timing(
        cycle_id=cycle_id, started_at=started, completed_at=cycle.completed_at,
        duration_seconds=total_duration, stage_timings=stage_timings,
    )
    _log("agent_cycle", **asdict(cycle))
    return status


def _heartbeat_loop(
    db_path: str,
    interval_seconds: float,
    stop_event: threading.Event,
    process_identity: dict[str, object],
) -> None:
    store = AgentStore(db_path)
    store.set_process_identity(process_identity)
    try:
        while not stop_event.wait(interval_seconds):
            try:
                store.refresh_heartbeat()
            except sqlite3.Error as exc:
                _log("agent_heartbeat_error", status="unavailable",
                     error=type(exc).__name__, **sqlite_error_fields(exc))
    finally:
        store.conn.close()


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
    bootstrap_state = bootstrap_hosted_bill_budget(config.db_path)
    if bootstrap_state is not None:
        _log("hosted_bill_budget_bootstrap", status=bootstrap_state)
    identity = identity or AgentIdentity()
    store = AgentStore(config.db_path)
    process_identity = asdict(capture_process_identity(config.db_path))
    store.set_process_identity(process_identity)
    runtime_session_id = (
        f"runtime:{process_identity['pid']}:{process_identity['started_at']}"
    )
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
    store.append_runtime_event(
        runtime_session_id,
        "agent_runtime",
        "started",
        json.dumps(process_identity, sort_keys=True),
    )
    _log("agent_started", agent_id=identity.agent_id, mission=identity.mission)

    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(config.db_path, config.heartbeat_interval_seconds, heartbeat_stop,
              process_identity),
        name="noema-agent-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    trench_sampler: asyncio.Task[None] | None = None
    market_discovery_sampler: asyncio.Task[None] | None = None
    polymarket_stream: asyncio.Task[None] | None = None
    polymarket_rest_sampler: asyncio.Task[None] | None = None
    changed_records = asyncio.Event()
    snapshot_lock = asyncio.Lock()
    runtime_loop = asyncio.get_running_loop()

    def request_snapshot_after_change(*_args: object) -> None:
        runtime_loop.call_soon_threadsafe(changed_records.set)

    changed_snapshot_task = asyncio.create_task(
        _changed_console_snapshot_loop(config.db_path, changed_records, snapshot_lock),
        name="noema-change-triggered-console-snapshot",
    )
    trench_config = TrenchCollectorConfig.from_env()
    if trench_config.enabled:
        trench_sampler = asyncio.create_task(
            _trench_sampler_loop(config.db_path, trench_config, request_snapshot_after_change),
            name="noema-trench-forward-sampler",
        )
        _log(
            "trench_forward_sampler_started",
            primary_rpc_configured=bool(os.getenv("NOEMA_SOLANA_RPC_URL", "").strip()),
            fallback_rpc_configured=bool(trench_config.solana_rpc_fallback_url),
            interval_seconds=trench_config.sample_interval_seconds,
            enabled=True,
        )
    else:
        _log("trench_forward_sampler_started", enabled=False, reason="collector_disabled")
    market_discovery_config = MarketDiscoveryConfig.from_env()
    if market_discovery_config.enabled:
        market_discovery_sampler = asyncio.create_task(
            _market_discovery_sampler_loop(
                config.db_path, market_discovery_config, request_snapshot_after_change,
            ),
            name="noema-market-pair-discovery",
        )
        _log(
            "market_pair_discovery_started",
            enabled=True,
            interval_seconds=market_discovery_config.interval_seconds,
            token_limit=market_discovery_config.token_limit,
            pair_limit_per_token=market_discovery_config.pair_limit_per_token,
            execution_authority="disabled",
        )
    else:
        _log("market_pair_discovery_started", enabled=False, reason="collector_disabled")
    if os.getenv("NOEMA_POLYMARKET_US_ENABLED", "1").strip() == "1":
        polymarket_stream = asyncio.create_task(
            run_polymarket_account_stream(
                lambda: config.db_path, request_snapshot_after_change,
            ),
            name="noema-polymarket-account-stream",
        )
        polymarket_rest_sampler = asyncio.create_task(
            _polymarket_account_sampler(config.db_path, request_snapshot_after_change),
            name="noema-polymarket-account-rest-sampler",
        )
        key_id_present, secret_present = polymarket_us_credentials_present()
        _log(
            "polymarket_private_sampler_started",
            credential_key_id_present=key_id_present,
            credential_secret_present=secret_present,
            stream_task=True, rest_sampler_task=True,
            rest_interval_seconds=_polymarket_account_sample_interval(),
        )
    else:
        _log("polymarket_private_sampler_disabled", flag="NOEMA_POLYMARKET_US_ENABLED")

    next_outcome_sync = 0.0
    first_cycle_event_written = False
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
                cycle_status = await run_cycle(
                    cycle_id=cycle_id, config=config, identity=identity, store=store,
                )
                if not first_cycle_event_written and cycle_status is not None:
                    cycle = cycle_status.last_cycle
                    store.append_runtime_event(
                        runtime_session_id,
                        "agent_cycle",
                        "completed",
                        json.dumps({
                            "cycle_id": None if cycle is None else cycle.cycle_id,
                            "health": "unknown" if cycle is None else cycle.health,
                        }, sort_keys=True),
                    )
                    first_cycle_event_written = True
            except (httpx.HTTPError, sqlite3.Error, OSError, RuntimeError, ValueError,
                    KeyError, TypeError) as exc:
                _log("agent_cycle_error", error=type(exc).__name__, cycle_id=cycle_id,
                     **sqlite_error_fields(exc))
            try:
                async with snapshot_lock:
                    snapshot_status = await publish_console_snapshot(config.db_path)
            except Exception as exc:  # noqa: BLE001 - snapshot diagnostics must not stop collection.
                # Keep the worker alive and report only a safe exception class.
                snapshot_status = {
                    "status": "unavailable",
                    "failure_stage": "worker_snapshot_runtime",
                    "failure_classification": "snapshot_publish_failure",
                    "error_type": type(exc).__name__,
                }
            _log("agent_console_snapshot", **snapshot_status, cycle_id=cycle_id)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, config.cycle_interval_seconds - elapsed))
    finally:
        for task in (
            trench_sampler, market_discovery_sampler, polymarket_stream,
            polymarket_rest_sampler, changed_snapshot_task,
        ):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=max(5.0, config.heartbeat_interval_seconds + 1))

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
        store.append_runtime_event(
            runtime_session_id, "agent_runtime", "stopped", "runtime shutdown completed",
        )
        _log("agent_stopped", agent_id=identity.agent_id)
