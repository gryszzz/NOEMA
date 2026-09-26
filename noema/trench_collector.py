from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .solana_research import (
    JupiterTrenchResearchClient,
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
        self.conn.commit()

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
                    SELECT COUNT(*) FROM trench_collection_attempts
                    WHERE mint = ? AND horizon_seconds = ?
                    """,
                    (mint, horizon),
                ).fetchone()
                if attempts is not None and int(attempts[0]) >= max_attempts:
                    continue
                due.append(DueObservation(str(mint), first_pool, horizon, scheduled))
                if len(due) >= limit:
                    self.conn.commit()
                    return due
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
        if status not in {"recorded", "unavailable", "error"}:
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
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO trench_observations (
                mint, horizon_seconds, scheduled_at, observed_at,
                tick_json, control_json, raw_token_json, holder_shares_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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


async def collect_trench_cycle(
    *,
    db_path: str,
    jupiter: JupiterTrenchResearchClient,
    solana: SolanaRpcResearchClient,
    now: datetime | None = None,
    due_limit: int = 20,
    enrichment_limit: int = 2,
    request_pause_seconds: float = 0.0,
) -> TrenchCollectionSummary:
    """Run one bounded discovery + snapshot cycle without submitting transactions."""

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    if due_limit <= 0 or enrichment_limit < 0 or request_pause_seconds < 0:
        raise ValueError("invalid collector limits")

    store = TrenchCollectorStore(db_path)
    recent = await jupiter.recent_tradeable_tokens()
    discovered = store.register_recent(recent, now=now)
    due = store.due_observations(now=now, limit=due_limit)
    if not due:
        return TrenchCollectionSummary(discovered, 0, 0, 0, 0, 0, 0)

    if request_pause_seconds:
        await asyncio.sleep(request_pause_seconds)

    by_mint = await jupiter.tokens_by_mint(tuple(item.mint for item in due))
    recorded = unavailable = failed = assessments = counterfactuals = 0
    enrichment_used = 0

    for item in due:
        token = by_mint.get(item.mint)
        if token is None:
            store.record_attempt(
                item,
                status="unavailable",
                detail="token absent from Jupiter search response",
                attempted_at=now,
            )
            unavailable += 1
            continue

        holder_shares: tuple[float, ...] = ()
        enrichment_detail: str | None = None
        if (
            item.horizon_seconds in ENRICHMENT_HORIZONS
            and enrichment_used < enrichment_limit
        ):
            enrichment_used += 1
            try:
                holder_shares = await solana.top_account_supply_shares(item.mint)
            except (httpx.HTTPError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                enrichment_detail = f"holder enrichment unavailable: {type(exc).__name__}"

        try:
            tick = jupiter_launch_tick(
                token,
                observed_at=now,
                holder_shares=holder_shares,
            )
            control = jupiter_control_state(token)
            inserted = store.record_observation(
                item,
                tick=tick,
                control=control,
                raw_token=token,
                holder_shares=holder_shares,
            )
            store.record_attempt(
                item,
                status="recorded",
                detail=enrichment_detail,
                raw=token,
                attempted_at=now,
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
            store.record_attempt(
                item,
                status="error",
                detail=f"normalization failed: {type(exc).__name__}",
                raw=token,
                attempted_at=now,
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
    )
