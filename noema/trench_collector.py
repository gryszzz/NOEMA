from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .solana_research import (
    JupiterTrenchResearchClient,
    ProviderFailure,
    SolanaRpcResearchClient,
    jupiter_control_state,
    jupiter_first_pool_at,
    jupiter_launch_tick,
)
from .trench_features import extract_trench_features
from .trench_models import LaunchTick, TokenControlState
from .trench_risk import assess_trench_candidate
from .trench_store import TrenchResearchStore

DEFAULT_HORIZONS = (30, 60, 120, 300, 900, 3600, 21_600, 86_400)
ASSESSMENT_HORIZON = 300
ENRICHMENT_HORIZONS = frozenset({300, 3600, 86_400})
_SAFE_NORMALIZATION_REASONS = {
    "Jupiter token price unavailable": "price_unavailable",
    "Jupiter token liquidity unavailable": "liquidity_unavailable",
    "observation precedes first pool": "observation_precedes_launch",
    "stats24h launch normalization only supports <=24h tokens": "launch_age_out_of_range",
    "stale or premature observation rejected": "stale_or_premature_observation",
    "provider returned a different token than the scheduled launch": "token_identity_mismatch",
}


def horizon_lateness_seconds(horizon_seconds: int) -> float:
    """Allowed observation delay before a target horizon is considered missed."""

    return max(30.0, min(300.0, horizon_seconds * 0.25))


@dataclass(frozen=True)
class DueObservation:
    mint: str
    first_pool_at: datetime
    horizon_seconds: int
    scheduled_at: datetime


@dataclass(frozen=True)
class TrenchCollectionSummary:
    discovered: int
    due: int
    recorded: int
    unavailable: int
    failed: int
    assessments_recorded: int
    counterfactuals_recorded: int
    provider_failures: tuple[str, ...] = ()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _tick_payload(tick: LaunchTick) -> str:
    return _json(asdict(tick))


def _control_payload(control: TokenControlState) -> str:
    return _json(asdict(control))


def _tick_from_payload(payload: str) -> LaunchTick:
    raw = json.loads(payload)
    raw["observed_at"] = datetime.fromisoformat(str(raw["observed_at"]))
    raw["holder_shares"] = tuple(float(value) for value in raw.get("holder_shares", ()))
    return LaunchTick(**raw)


def _control_from_payload(payload: str) -> TokenControlState:
    return TokenControlState(**json.loads(payload))


class TrenchCollectorStore:
    """Immutable launch discovery/snapshot store with separate failed-attempt history."""

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trench_launches (
                mint TEXT PRIMARY KEY,
                first_pool_at TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                discovery_json TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0, 1))
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trench_observations (
                mint TEXT NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                scheduled_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                tick_json TEXT NOT NULL,
                control_json TEXT NOT NULL,
                raw_token_json TEXT NOT NULL,
                holder_shares_json TEXT NOT NULL,
                provider_provenance_json TEXT,
                PRIMARY KEY (mint, horizon_seconds),
                FOREIGN KEY(mint) REFERENCES trench_launches(mint)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trench_collection_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                horizon_seconds INTEGER NOT NULL,
                attempted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                raw_json TEXT,
                FOREIGN KEY(mint) REFERENCES trench_launches(mint)
            )
            """
        )
        columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(trench_observations)"
        )}
        if "provider_provenance_json" not in columns:
            # Existing snapshots remain explicitly unattributed; provenance cannot
            # be reconstructed safely after the fact.
            self.conn.execute(
                "ALTER TABLE trench_observations ADD COLUMN provider_provenance_json TEXT"
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def register_recent(
        self,
        rows: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        added = 0

        for row in rows:
            mint = row.get("id") or row.get("address")
            first_pool = jupiter_first_pool_at(row)
            if (
                not isinstance(mint, str)
                or not mint.strip()
                or first_pool is None
                or first_pool > now
                or now - first_pool > timedelta(days=1)
            ):
                continue
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO trench_launches (
                    mint, first_pool_at, discovered_at, last_seen_at,
                    discovery_json, active
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    mint.strip(),
                    first_pool.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    _json(row),
                ),
            )
            added += int(cursor.rowcount == 1)
            if cursor.rowcount == 0:
                self.conn.execute(
                    """
                    UPDATE trench_launches
                    SET last_seen_at = ?, active = 1
                    WHERE mint = ?
                    """,
                    (now.isoformat(), mint.strip()),
                )
        self.conn.commit()
        return added

    def due_observations(
        self,
        *,
        now: datetime | None = None,
        horizons: tuple[int, ...] = DEFAULT_HORIZONS,
        limit: int = 25,
        max_attempts: int = 3,
    ) -> list[DueObservation]:
        if limit <= 0 or max_attempts <= 0:
            return []
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)

        rows = self.conn.execute(
            """
            SELECT mint, first_pool_at
            FROM trench_launches
            WHERE active = 1
            ORDER BY first_pool_at ASC, mint
            """
        ).fetchall()

        due: list[DueObservation] = []
        for mint, first_pool_raw in rows:
            first_pool = datetime.fromisoformat(str(first_pool_raw))
            if first_pool.tzinfo is None:
                continue
            first_pool = first_pool.astimezone(UTC)
            if now - first_pool > timedelta(days=1, hours=1):
                self.conn.execute(
                    "UPDATE trench_launches SET active = 0 WHERE mint = ?",
                    (mint,),
                )
                continue
            for horizon in horizons:
                if horizon <= 0:
                    continue
                scheduled = first_pool + timedelta(seconds=horizon)
                if scheduled > now:
                    continue
                missed = self.conn.execute(
                    """
                    SELECT 1 FROM trench_collection_attempts
                    WHERE mint = ? AND horizon_seconds = ? AND status = 'missed'
                    LIMIT 1
                    """,
                    (mint, horizon),
                ).fetchone()
                if missed is not None:
                    continue
                if (now - scheduled).total_seconds() > horizon_lateness_seconds(horizon):
                    self.record_attempt(
                        DueObservation(str(mint), first_pool, horizon, scheduled),
                        status="missed",
                        detail="target horizon exceeded lateness tolerance before collection",
                        attempted_at=now,
                    )
                    continue
                exists = self.conn.execute(
                    """
                    SELECT 1 FROM trench_observations
                    WHERE mint = ? AND horizon_seconds = ?
                    """,
                    (mint, horizon),
                ).fetchone()
                if exists is not None:
                    continue
                attempts = self.conn.execute(
                    """
                    SELECT COUNT(*), MAX(attempted_at) FROM trench_collection_attempts
                    WHERE mint = ? AND horizon_seconds = ? AND status IN ('error','unavailable')
                    """,
                    (mint, horizon),
                ).fetchone()
                failures = int(attempts[0]) if attempts else 0
                if failures and attempts[1]:
                    delay = min(120.0, 10.0 * (2 ** min(failures - 1, 4)))
                    try:
                        latest_attempt = datetime.fromisoformat(str(attempts[1])).astimezone(UTC)
                    except (TypeError, ValueError):
                        latest_attempt = now
                    if (now - latest_attempt).total_seconds() < delay:
                        continue
                # max_attempts remains accepted for API compatibility, but a temporary
                # outage never permanently abandons a still-on-time observation.
                due.append(DueObservation(str(mint), first_pool, horizon, scheduled))
                if len(due) >= limit:
                    self.conn.commit()
                    return due
                # At most one target snapshot per mint per collector cycle.
                break
        self.conn.commit()
        return due

    def record_attempt(
        self,
        due: DueObservation,
        *,
        status: str,
        detail: str | None = None,
        raw: dict[str, Any] | None = None,
        attempted_at: datetime | None = None,
    ) -> int:
        if status not in {"recorded", "unavailable", "error", "missed"}:
            raise ValueError("invalid collection-attempt status")
        attempted_at = attempted_at or datetime.now(UTC)
        if attempted_at.tzinfo is None:
            raise ValueError("attempted_at must be timezone-aware")
        cursor = self.conn.execute(
            """
            INSERT INTO trench_collection_attempts (
                mint, horizon_seconds, attempted_at, status, detail, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                due.mint,
                due.horizon_seconds,
                attempted_at.astimezone(UTC).isoformat(),
                status,
                detail,
                None if raw is None else _json(raw),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def record_observation(
        self,
        due: DueObservation,
        *,
        tick: LaunchTick,
        control: TokenControlState,
        raw_token: dict[str, Any],
        holder_shares: tuple[float, ...],
        provider_provenance: dict[str, Any] | None = None,
    ) -> bool:
        if due.first_pool_at.tzinfo is None or due.scheduled_at.tzinfo is None:
            raise ValueError("observation schedule must be timezone-aware")
        if due.horizon_seconds <= 0:
            raise ValueError("observation horizon must be positive")
        first_pool = due.first_pool_at.astimezone(UTC)
        scheduled = due.scheduled_at.astimezone(UTC)
        if scheduled != first_pool + timedelta(seconds=due.horizon_seconds):
            raise ValueError("observation schedule does not match its launch horizon")
        observed = tick.observed_at.astimezone(UTC)
        if (not tick.price_usd > 0 or not math.isfinite(tick.price_usd)
                or not math.isfinite(tick.liquidity_usd)):
            raise ValueError("invalid observation price or liquidity")
        lateness = horizon_lateness_seconds(due.horizon_seconds)
        if observed < scheduled or (observed - scheduled).total_seconds() > lateness:
            raise ValueError("stale or premature observation rejected")
        token_mint = raw_token.get("id") or raw_token.get("address")
        if not isinstance(token_mint, str) or not token_mint.strip():
            raise ValueError("provider token identity is missing")
        if token_mint.strip() != due.mint:
            raise ValueError("provider returned a different token than the scheduled launch")
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO trench_observations (
                mint, horizon_seconds, scheduled_at, observed_at,
                tick_json, control_json, raw_token_json, holder_shares_json,
                provider_provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                due.mint,
                due.horizon_seconds,
                due.scheduled_at.astimezone(UTC).isoformat(),
                tick.observed_at.astimezone(UTC).isoformat(),
                _tick_payload(tick),
                _control_payload(control),
                _json(raw_token),
                _json(holder_shares),
                None if provider_provenance is None else _json(provider_provenance),
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def observations(
        self,
        mint: str,
        *,
        max_horizon_seconds: int | None = None,
    ) -> list[tuple[int, LaunchTick, TokenControlState]]:
        sql = """
            SELECT horizon_seconds, tick_json, control_json
            FROM trench_observations
            WHERE mint = ?
        """
        params: list[object] = [mint.strip()]
        if max_horizon_seconds is not None:
            sql += " AND horizon_seconds <= ?"
            params.append(max_horizon_seconds)
        sql += " ORDER BY horizon_seconds ASC"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [
            (
                int(horizon),
                _tick_from_payload(str(tick_json)),
                _control_from_payload(str(control_json)),
            )
            for horizon, tick_json, control_json in rows
        ]

    def counts(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for name, table in (
            ("launches", "trench_launches"),
            ("observations", "trench_observations"),
            ("attempts", "trench_collection_attempts"),
        ):
            row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            values[name] = 0 if row is None else int(row[0])
        return values

    def pending_reconciliation_mints(self) -> list[str]:
        """Find persisted snapshots whose derived assessment/outcome may be unfinished."""
        tables = {str(row[0]) for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"trench_candidates", "trench_counterfactuals"} <= tables:
            return []
        return [str(row[0]) for row in self.conn.execute(
            """SELECT DISTINCT o.mint FROM trench_observations o
               WHERE o.horizon_seconds=300 AND (
                 (NOT EXISTS (SELECT 1 FROM trench_candidates c WHERE c.token_mint=o.mint)
                  AND (SELECT COUNT(*) FROM trench_observations early
                       WHERE early.mint=o.mint AND early.horizon_seconds<=300) >= 2)
                 OR EXISTS (SELECT 1 FROM trench_observations one
                   JOIN trench_candidates c ON c.token_mint=o.mint
                   WHERE one.mint=o.mint AND one.horizon_seconds=3600
                     AND NOT EXISTS (SELECT 1 FROM trench_counterfactuals cf
                       WHERE cf.candidate_id=c.candidate_id AND cf.horizon_seconds=3600))
               ) ORDER BY o.mint""")
        ]


def _max_drawdown(prices: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            worst = max(worst, (peak - price) / peak)
    return worst


def update_trench_research_from_observations(
    db_path: str,
    *,
    mint: str,
) -> tuple[int, int]:
    """Create one fixed 5m assessment and later launch-age counterfactual labels."""

    collector = TrenchCollectorStore(db_path)
    research = TrenchResearchStore(db_path)
    try:
        return _update_trench_research_with_stores(collector, research, mint=mint)
    finally:
        collector.close()
        research.conn.close()


def _update_trench_research_with_stores(
    collector: TrenchCollectorStore,
    research: TrenchResearchStore,
    *,
    mint: str,
) -> tuple[int, int]:
    assessments = 0
    counterfactuals = 0

    candidate = research.candidate_for_token(mint)
    early = collector.observations(mint, max_horizon_seconds=ASSESSMENT_HORIZON)
    if candidate is None and len(early) >= 2 and early[-1][0] >= ASSESSMENT_HORIZON:
        _, last_tick, last_control = early[-1]
        features = extract_trench_features([tick for _, tick, _ in early])
        assessment = assess_trench_candidate(
            features,
            last_control,
            current_liquidity_usd=last_tick.liquidity_usd,
        )
        research.record_candidate(
            token_mint=mint,
            reference_price_usd=last_tick.price_usd,
            reference_liquidity_usd=last_tick.liquidity_usd,
            assessment=assessment,
            captured_at=last_tick.observed_at,
        )
        assessments += 1
        candidate = research.candidate_for_token(mint)

    if candidate is None:
        return assessments, counterfactuals

    candidate_id, captured_at, reference_price = candidate
    later = [
        (horizon, tick)
        for horizon, tick, _ in collector.observations(mint)
        if horizon > ASSESSMENT_HORIZON and tick.observed_at >= captured_at
    ]
    path_prices = [reference_price]
    for horizon, tick in later:
        path_prices.append(tick.price_usd)
        if research.has_counterfactual(candidate_id, horizon):
            continue
        final_return = (
            (tick.price_usd - reference_price) / reference_price
            if reference_price > 0
            else 0.0
        )
        max_return = (
            (max(path_prices) - reference_price) / reference_price
            if reference_price > 0
            else 0.0
        )
        research.record_counterfactual(
            candidate_id=candidate_id,
            horizon_seconds=horizon,
            final_return_fraction=final_return,
            max_return_fraction=max_return,
            max_drawdown_fraction=_max_drawdown(path_prices),
            observed_at=tick.observed_at,
        )
        counterfactuals += 1

    return assessments, counterfactuals


async def _collect_trench_cycle(
    *,
    db_path: str,
    store: TrenchCollectorStore,
    jupiter: JupiterTrenchResearchClient,
    solana: SolanaRpcResearchClient,
    now: datetime | None = None,
    due_limit: int = 20,
    enrichment_limit: int = 2,
    request_pause_seconds: float = 0.0,
    discover_new: bool = True,
) -> TrenchCollectionSummary:
    """Collect authorized new launches and complete already-scheduled observations.

    Disabling discovery preserves forward outcome collection, including unfavorable
    observations after a specialist loses its discretionary research allocation.
    """

    fixed_clock = now is not None
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    if due_limit <= 0 or enrichment_limit < 0 or request_pause_seconds < 0:
        raise ValueError("invalid collector limits")
    if type(discover_new) is not bool:
        raise ValueError("discover_new must be a boolean")

    discovered = 0
    provider_failures: list[str] = []
    assessments = counterfactuals = 0
    # Observation and derived-label writes are separate durable transactions. Replay
    # only derivation from persisted, already time-validated snapshots after a restart.
    for mint in store.pending_reconciliation_mints():
        added_assessments, added_counterfactuals = update_trench_research_from_observations(
            db_path, mint=mint,
        )
        assessments += added_assessments
        counterfactuals += added_counterfactuals
    if discover_new:
        try:
            recent = await jupiter.recent_tradeable_tokens()
            discovered = store.register_recent(recent, now=now)
        except ProviderFailure as exc:
            provider_failures.append(f"{exc.provider}:{exc.error_class}")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            provider_failures.append(f"jupiter:{type(exc).__name__}")
    due = store.due_observations(now=now, limit=due_limit)
    if not due:
        return TrenchCollectionSummary(
            discovered, 0, 0, 0, 0, assessments, counterfactuals, tuple(provider_failures),
        )

    if request_pause_seconds:
        await asyncio.sleep(request_pause_seconds)

    try:
        by_mint = await jupiter.tokens_by_mint(tuple(item.mint for item in due))
    except ProviderFailure as exc:
        provider_failures.append(f"{exc.provider}:{exc.error_class}")
        for item in due:
            store.record_attempt(
                item, status="unavailable",
                detail=f"provider unavailable: {exc.provider}:{exc.error_class}",
                attempted_at=now if fixed_clock else datetime.now(UTC),
            )
        return TrenchCollectionSummary(
            discovered, len(due), 0, len(due), 0, 0, 0, tuple(provider_failures),
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        error_class = type(exc).__name__
        provider_failures.append(f"jupiter:{error_class}")
        for item in due:
            store.record_attempt(
                item, status="unavailable", detail=f"provider unavailable: jupiter:{error_class}",
                attempted_at=now if fixed_clock else datetime.now(UTC),
            )
        return TrenchCollectionSummary(
            discovered, len(due), 0, len(due), 0, 0, 0, tuple(provider_failures),
        )
    jupiter_received_at = now if fixed_clock else datetime.now(UTC)
    recorded = unavailable = failed = 0
    enrichment_used = 0

    for item in due:
        token = by_mint.get(item.mint)
        if token is None:
            store.record_attempt(
                item,
                status="unavailable",
                detail="token absent from Jupiter search response",
                attempted_at=now if fixed_clock else datetime.now(UTC),
            )
            unavailable += 1
            continue

        holder_shares: tuple[float, ...] = ()
        enrichment_detail: str | None = None
        enrichment_error_class: str | None = None
        if (
            item.horizon_seconds in ENRICHMENT_HORIZONS
            and enrichment_used < enrichment_limit
        ):
            enrichment_used += 1
            try:
                holder_shares = await solana.top_account_supply_shares(item.mint)
                rpc_attempt = getattr(solana, "last_attempt_provenance", None) or {}
                failed_providers = rpc_attempt.get("failed_providers", [])
                failure_summary = ", ".join(
                    f"{failure.get('source_alias', 'provider')}:{failure.get('error_class', 'failure')}"
                    + (f" HTTP {failure['http_status']}" if failure.get("http_status") else "")
                    for failure in failed_providers if isinstance(failure, dict)
                )
                enrichment_detail = (
                    f"holder enrichment recorded: solana_rpc:{solana.last_provider or 'unknown'}"
                    + (f" (fallback after {failure_summary})" if failure_summary else "")
                )
            except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                error_class = (exc.error_class if isinstance(exc, ProviderFailure)
                               else type(exc).__name__)
                enrichment_error_class = error_class
                http_status = getattr(exc, "http_status", None)
                rpc_attempt = getattr(solana, "last_attempt_provenance", None) or {}
                failed_providers = rpc_attempt.get("failed_providers", [])
                failure_summary = ", ".join(
                    f"{failure.get('source_alias', 'provider')}:{failure.get('error_class', 'failure')}"
                    + (f" HTTP {failure['http_status']}" if failure.get("http_status") else "")
                    for failure in failed_providers if isinstance(failure, dict)
                )
                if not failure_summary:
                    failure_summary = f"{error_class}" + (f" HTTP {http_status}" if http_status else "")
                fallback_note = (
                    "; fallback not configured"
                    if rpc_attempt and len(
                        getattr(solana, "providers", lambda: (("primary", None),))()
                    ) < 2 else ""
                )
                enrichment_detail = f"holder enrichment unavailable: solana_rpc:{failure_summary}{fallback_note}"
                provider_failures.append(f"solana_rpc:{error_class}")

        try:
            tick = jupiter_launch_tick(
                token,
                observed_at=jupiter_received_at,
                holder_shares=holder_shares,
            )
            control = jupiter_control_state(token)
            provenance = {
                "observation": {"provider": "jupiter", "source_alias": "primary",
                                "received_at": jupiter_received_at.isoformat(),
                                "freshness_basis": "local_response_received_at",
                                "upstream_quote_timestamp": "unavailable"},
                "fields": {
                    "first_pool_at": "jupiter.firstPool",
                    "price_usd": "jupiter.usdPrice",
                    "liquidity_usd": "jupiter.liquidity",
                    "flow_and_participant_metrics": "jupiter.stats24h",
                    "token_controls": "jupiter.audit",
                    "holder_shares": (f"solana_rpc.{solana.last_provider}"
                                       if holder_shares else None),
                },
                "solana_rpc": (
                    {"status": "recorded", "source_alias": solana.last_provider,
                     "context_slot": solana.last_context_slot,
                     **(getattr(solana, "last_attempt_provenance", None) or {})}
                    if holder_shares else
                    {"status": ("unavailable" if "unavailable" in (enrichment_detail or "")
                                else "no_data" if enrichment_detail else "not_queried"),
                     "error_class": enrichment_error_class,
                     **(getattr(solana, "last_attempt_provenance", None) or {})}
                ),
            }
            inserted = store.record_observation(
                item,
                tick=tick,
                control=control,
                raw_token=token,
                holder_shares=holder_shares,
                provider_provenance=provenance,
            )
            store.record_attempt(
                item,
                status="recorded",
                detail=enrichment_detail,
                raw=token,
                attempted_at=datetime.now(UTC) if not fixed_clock else tick.observed_at,
            )
            if inserted:
                recorded += 1
                added_assessments, added_counterfactuals = (
                    update_trench_research_from_observations(
                        db_path,
                        mint=item.mint,
                    )
                )
                assessments += added_assessments
                counterfactuals += added_counterfactuals
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            reason = _SAFE_NORMALIZATION_REASONS.get(str(exc), "invalid_provider_or_observation_data")
            store.record_attempt(
                item,
                status="error",
                detail=f"normalization failed: {type(exc).__name__} ({reason})",
                raw=token,
                attempted_at=now if fixed_clock else datetime.now(UTC),
            )
            failed += 1

    return TrenchCollectionSummary(
        discovered=discovered,
        due=len(due),
        recorded=recorded,
        unavailable=unavailable,
        failed=failed,
        assessments_recorded=assessments,
        counterfactuals_recorded=counterfactuals,
        provider_failures=tuple(provider_failures),
    )


async def collect_trench_cycle(
    *,
    db_path: str,
    jupiter: JupiterTrenchResearchClient,
    solana: SolanaRpcResearchClient,
    now: datetime | None = None,
    due_limit: int = 20,
    enrichment_limit: int = 2,
    request_pause_seconds: float = 0.0,
    discover_new: bool = True,
) -> TrenchCollectionSummary:
    """Run one bounded collection cycle and close its persistent-store handle."""
    store = TrenchCollectorStore(db_path)
    try:
        return await _collect_trench_cycle(
            db_path=db_path, store=store, jupiter=jupiter, solana=solana, now=now,
            due_limit=due_limit, enrichment_limit=enrichment_limit,
            request_pause_seconds=request_pause_seconds, discover_new=discover_new,
        )
    finally:
        store.close()
