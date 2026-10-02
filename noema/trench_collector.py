from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import weakref
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .solana_research import (
    DexScreenerTrenchPriceClient,
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
_COLLECTOR_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()
ASSESSMENT_HORIZON = 300
ENRICHMENT_HORIZONS = frozenset({300, 3600, 86_400})
# Price, liquidity, token identity and timestamp are required for a valid label.
# Holder shares, flow, audit and organic-score fields are optional inputs.
REQUIRED_OBSERVATION_FIELDS = frozenset({
    "token_identity", "price_usd", "liquidity_usd", "observed_at",
})
OPTIONAL_ENRICHMENT_FIELDS = frozenset({
    "holder_shares", "flow_metrics", "token_audit", "organic_score",
})
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
    provider_health: tuple[str, ...] = ()


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


def _positive_market_value(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _retry_delay_seconds(identity: str, horizon: int, failure_count: int) -> float:
    base = min(120.0, 10.0 * (2 ** min(max(0, failure_count - 1), 4)))
    digest = hashlib.sha256(f"{identity}:{horizon}:{failure_count}".encode()).digest()
    jitter = 0.9 + int.from_bytes(digest[:4], "big") / 0xFFFFFFFF * 0.2
    return base * jitter


def _provider_retry_delay_seconds(provider: str, failure_count: int) -> float:
    base = min(300.0, 15.0 * (2 ** min(max(0, failure_count - 1), 5)))
    digest = hashlib.sha256(f"{provider}:{failure_count}".encode()).digest()
    jitter = 0.9 + int.from_bytes(digest[:4], "big") / 0xFFFFFFFF * 0.2
    return base * jitter


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
        attempt_columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(trench_collection_attempts)"
        )}
        if "retry_at" not in attempt_columns:
            self.conn.execute("ALTER TABLE trench_collection_attempts ADD COLUMN retry_at TEXT")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS trench_provider_health (
                provider TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_failure_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_error_class TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_trench_attempt_target ON trench_collection_attempts(mint,horizon_seconds,id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_trench_observation_target ON trench_observations(mint,horizon_seconds)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS trench_candidate_maturities (
                candidate_id TEXT PRIMARY KEY,
                target_at TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                next_retry_at TEXT,
                detail TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        self._backfill_legacy_solana_provider_health()
        self._backfill_candidate_maturities()
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

    def _backfill_legacy_solana_provider_health(self) -> None:
        """Project legacy optional RPC enrichment outcomes into canonical health.

        Older collector versions persisted enrichment failures in attempt details but
        did not maintain provider health rows. Preserve the most recent evidence so
        the dashboard does not report an unknown/healthy provider after a restart.
        Existing canonical rows always win.
        """
        rows = self.conn.execute(
            """SELECT attempted_at,detail FROM trench_collection_attempts
               WHERE detail LIKE 'holder enrichment recorded:%'
                  OR detail LIKE 'holder enrichment unavailable:%'
               ORDER BY attempted_at DESC"""
        ).fetchall()
        if not rows or self.conn.execute(
            "SELECT 1 FROM trench_provider_health WHERE provider='solana_rpc:primary'"
        ).fetchone():
            return

        def is_success(detail: str) -> bool:
            return detail.startswith("holder enrichment recorded:")

        latest_at, latest_detail = str(rows[0][0]), str(rows[0][1] or "")
        latest_success = next(
            (str(attempted_at) for attempted_at, detail in rows if is_success(str(detail or ""))),
            None,
        )
        latest_failure = next(
            (str(attempted_at) for attempted_at, detail in rows if not is_success(str(detail or ""))),
            None,
        )
        consecutive_failures = 0
        for _, detail in rows:
            if is_success(str(detail or "")):
                break
            consecutive_failures += 1
        healthy = is_success(latest_detail)
        error_class = None
        if not healthy:
            tail = latest_detail.split("holder enrichment unavailable:", 1)[-1]
            tail = tail.split("fallback", 1)[0].strip(" ;")
            error_class = tail.rsplit(":", 1)[-1] if tail else "provider_failure"
        self.conn.execute(
            """INSERT OR IGNORE INTO trench_provider_health (
                 provider,state,last_attempt_at,last_success_at,last_failure_at,
                 consecutive_failures,next_retry_at,last_error_class,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            ("solana_rpc:primary", "healthy" if healthy else "degraded", latest_at,
             latest_success, latest_failure, 0 if healthy else consecutive_failures,
             None, error_class, latest_at),
        )

    def _backfill_candidate_maturities(self) -> None:
        tables = {str(row[0]) for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"trench_candidates", "trench_launches"} <= tables:
            return
        rows = self.conn.execute(
            """SELECT c.candidate_id,c.token_mint,l.first_pool_at
               FROM trench_candidates c JOIN trench_launches l ON l.mint=c.token_mint
               LEFT JOIN trench_candidate_maturities m ON m.candidate_id=c.candidate_id
               WHERE m.candidate_id IS NULL"""
        ).fetchall()

        now = datetime.now(UTC)
        for candidate_id, mint, first_pool_raw in rows:
            try:
                target = datetime.fromisoformat(str(first_pool_raw)).astimezone(UTC) + timedelta(seconds=3600)
            except (TypeError, ValueError):
                continue
            has_label = self.conn.execute(
                "SELECT 1 FROM trench_counterfactuals WHERE candidate_id=? AND horizon_seconds=3600",
                (candidate_id,),
            ).fetchone() if "trench_counterfactuals" in tables else None
            missed = self.conn.execute(
                "SELECT 1 FROM trench_collection_attempts WHERE mint=? AND horizon_seconds=3600 AND status='missed' LIMIT 1",
                (mint,),
            ).fetchone()
            state = "RECORDED" if has_label else "MISSED" if missed else "DUE" if target <= now else "PENDING"
            self.conn.execute(
                "INSERT OR IGNORE INTO trench_candidate_maturities (candidate_id,target_at,state,updated_at) VALUES (?,?,?,?)",
                (candidate_id, target.isoformat(), state, now.isoformat()),
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

        observed_keys = {(str(mint), int(horizon)) for mint, horizon in self.conn.execute(
            "SELECT mint,horizon_seconds FROM trench_observations"
        )}
        missed_keys = {(str(mint), int(horizon)) for mint, horizon in self.conn.execute(
            "SELECT DISTINCT mint,horizon_seconds FROM trench_collection_attempts WHERE status='missed'"
        )}
        latest_attempts = {
            (str(mint), int(horizon)): (str(status), attempted_at, retry_at)
            for mint, horizon, status, attempted_at, retry_at in self.conn.execute(
                """SELECT a.mint,a.horizon_seconds,a.status,a.attempted_at,a.retry_at
                   FROM trench_collection_attempts a JOIN (
                     SELECT mint,horizon_seconds,MAX(id) AS id
                     FROM trench_collection_attempts GROUP BY mint,horizon_seconds
                   ) latest ON latest.id=a.id"""
            )
        }
        failure_counts = {(str(mint), int(horizon)): int(count)
                          for mint, horizon, count in self.conn.execute(
            """SELECT mint,horizon_seconds,COUNT(*) FROM trench_collection_attempts
               WHERE status IN ('error','unavailable') GROUP BY mint,horizon_seconds"""
        )}

        candidates: list[DueObservation] = []
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
                target = (str(mint), horizon)
                if target in missed_keys:
                    continue
                if target in observed_keys:
                    continue
                if (now - scheduled).total_seconds() > horizon_lateness_seconds(horizon):
                    self.record_attempt(
                        DueObservation(str(mint), first_pool, horizon, scheduled),
                        status="missed",
                        detail="target horizon exceeded lateness tolerance before collection",
                        attempted_at=now,
                    )
                    continue
                attempts = latest_attempts.get(target)
                if attempts and attempts[0] in {"error", "unavailable"}:
                    if attempts[2]:
                        try:
                            retry_at = datetime.fromisoformat(str(attempts[2])).astimezone(UTC)
                        except (TypeError, ValueError):
                            retry_at = now
                    else:
                        failures = failure_counts.get(target, 0)
                        delay = _retry_delay_seconds(str(mint), horizon, failures)
                        try:
                            retry_at = datetime.fromisoformat(str(attempts[1])).astimezone(UTC) + timedelta(seconds=delay)
                        except (TypeError, ValueError):
                            retry_at = now
                    if now < retry_at:
                        continue
                candidates.append(DueObservation(str(mint), first_pool, horizon, scheduled))
        # The earliest closing window is served first. At most one snapshot per
        # token per poll prevents one timestamp from posing as multiple samples.
        candidates.sort(key=lambda item: (
            item.scheduled_at + timedelta(seconds=horizon_lateness_seconds(item.horizon_seconds)),
            item.scheduled_at, item.mint, item.horizon_seconds,
        ))
        due: list[DueObservation] = []
        selected_mints: set[str] = set()
        for item in candidates:
            if item.mint in selected_mints:
                continue
            due.append(item)
            selected_mints.add(item.mint)
            if item.horizon_seconds == 3600:
                self.update_candidate_maturities(item.mint, state="DUE")
            if len(due) >= limit:
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
        attempted_at = attempted_at.astimezone(UTC)
        if status == "missed":
            prior = self.conn.execute(
                "SELECT id FROM trench_collection_attempts WHERE mint=? AND horizon_seconds=? AND status='missed' ORDER BY id LIMIT 1",
                (due.mint, due.horizon_seconds),
            ).fetchone()
            if prior:
                return int(prior[0])
        retry_at = None
        if status in {"error", "unavailable"}:
            failures = int(self.conn.execute(
                "SELECT COUNT(*) FROM trench_collection_attempts WHERE mint=? AND horizon_seconds=? AND status IN ('error','unavailable')",
                (due.mint, due.horizon_seconds),
            ).fetchone()[0]) + 1
            retry_at = (attempted_at + timedelta(
                seconds=_retry_delay_seconds(due.mint, due.horizon_seconds, failures)
            )).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO trench_collection_attempts (
                mint, horizon_seconds, attempted_at, status, detail, raw_json, retry_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                due.mint,
                due.horizon_seconds,
                attempted_at.isoformat(),
                status,
                detail,
                None if raw is None else _json(raw),
                retry_at,
            ),
        )
        self.conn.commit()
        if due.horizon_seconds == 3600:
            state = {"recorded": "RETRYING", "unavailable": "RETRYABLE",
                     "error": "RETRYABLE", "missed": "MISSED"}[status]
            self.update_candidate_maturities(
                due.mint, state=state, attempted_at=attempted_at,
                next_retry_at=retry_at,
                detail=detail,
            )
        return int(cursor.lastrowid)

    def provider_states(self) -> dict[str, dict[str, Any]]:
        return {str(row[0]): {
            "state": str(row[1]), "last_attempt_at": row[2], "last_success_at": row[3],
            "last_failure_at": row[4], "consecutive_failures": int(row[5]),
            "next_retry_at": row[6], "last_error_class": row[7],
        } for row in self.conn.execute(
            "SELECT provider,state,last_attempt_at,last_success_at,last_failure_at,consecutive_failures,next_retry_at,last_error_class FROM trench_provider_health"
        )}

    def provider_retry_active(self, provider: str, *, now: datetime | None = None) -> bool:
        row = self.conn.execute(
            "SELECT next_retry_at FROM trench_provider_health WHERE provider=? AND state='degraded'",
            (provider,),
        ).fetchone()
        if not row or not row[0]:
            return False
        try:
            return (now or datetime.now(UTC)).astimezone(UTC) < datetime.fromisoformat(
                str(row[0])
            ).astimezone(UTC)
        except (TypeError, ValueError):
            return False

    def record_provider_health(
        self, provider: str, *, success: bool, error_class: str | None = None,
        attempted_at: datetime | None = None,
    ) -> datetime | None:
        attempted_at = attempted_at or datetime.now(UTC)
        if attempted_at.tzinfo is None:
            raise ValueError("provider attempt time must be timezone-aware")
        attempted_at = attempted_at.astimezone(UTC)
        prior = self.conn.execute(
            "SELECT consecutive_failures FROM trench_provider_health WHERE provider=?",
            (provider,),
        ).fetchone()
        failures = 0 if success else (int(prior[0]) if prior else 0) + 1
        retry_at = None if success else attempted_at + timedelta(
            seconds=_provider_retry_delay_seconds(provider, failures)
        )
        self.conn.execute(
            """INSERT INTO trench_provider_health (
                 provider,state,last_attempt_at,last_success_at,last_failure_at,
                 consecutive_failures,next_retry_at,last_error_class,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider) DO UPDATE SET
                 state=excluded.state,last_attempt_at=excluded.last_attempt_at,
                 last_success_at=COALESCE(excluded.last_success_at,trench_provider_health.last_success_at),
                 last_failure_at=excluded.last_failure_at,
                 consecutive_failures=excluded.consecutive_failures,
                 next_retry_at=excluded.next_retry_at,last_error_class=excluded.last_error_class,
                 updated_at=excluded.updated_at""",
            (provider, "healthy" if success else "degraded", attempted_at.isoformat(),
             attempted_at.isoformat() if success else None,
             None if success else attempted_at.isoformat(), failures,
             None if retry_at is None else retry_at.isoformat(),
             None if success else error_class, attempted_at.isoformat()),
        )
        self.conn.commit()
        return retry_at

    def update_candidate_maturities(
        self, mint: str, *, state: str, attempted_at: datetime | None = None,
        next_retry_at: str | None = None, detail: str | None = None,
    ) -> None:
        if state not in {"PENDING", "DUE", "RETRYABLE", "RETRYING", "RECORDED", "MISSED"}:
            raise ValueError("invalid candidate maturity state")
        columns = {str(row[1]) for row in self.conn.execute(
            "PRAGMA table_info(trench_candidate_maturities)"
        )}
        if not columns:
            return
        if not {str(row[0]) for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )} >= {"trench_candidates"}:
            return
        attempted = None if attempted_at is None else attempted_at.astimezone(UTC).isoformat()
        self.conn.execute(
            """UPDATE trench_candidate_maturities SET state=?,attempts=attempts+?,
                 last_attempt_at=COALESCE(?,last_attempt_at),next_retry_at=?,detail=?,updated_at=?
               WHERE candidate_id IN (SELECT candidate_id FROM trench_candidates WHERE token_mint=?)""",
            (state, int(attempted is not None and state in {"RETRYABLE", "RETRYING", "RECORDED", "MISSED"}),
             attempted, next_retry_at, detail, datetime.now(UTC).isoformat(), mint),
        )
        self.conn.commit()

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

    if research.has_counterfactual(candidate_id, 3600):
        research.update_maturity(candidate_id, state="RECORDED")

    return assessments, counterfactuals


async def _collect_trench_cycle(
    *,
    db_path: str,
    store: TrenchCollectorStore,
    jupiter: JupiterTrenchResearchClient,
    solana: SolanaRpcResearchClient,
    price_fallback: DexScreenerTrenchPriceClient | None,
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
        if store.provider_retry_active("jupiter_discovery", now=now):
            provider_failures.append("jupiter:backoff_active")
        else:
            try:
                recent = await jupiter.recent_tradeable_tokens()
                discovered = store.register_recent(recent, now=now)
                store.record_provider_health("jupiter_discovery", success=True)
            except ProviderFailure as exc:
                provider_failures.append(f"{exc.provider}:{exc.error_class}")
                store.record_provider_health("jupiter_discovery", success=False,
                                             error_class=exc.error_class)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                provider_failures.append(f"jupiter:{type(exc).__name__}")
                store.record_provider_health("jupiter_discovery", success=False,
                                             error_class=type(exc).__name__)
    due = store.due_observations(now=now, limit=due_limit)
    if not due:
        return TrenchCollectionSummary(
            discovered, 0, 0, 0, 0, assessments, counterfactuals,
            tuple(provider_failures),
            tuple(f"{provider}={state['state']}" for provider, state
                  in sorted(store.provider_states().items())),
        )

    for item in due:
        if item.horizon_seconds == 3600:
            store.update_candidate_maturities(
                item.mint, state="RETRYING", attempted_at=now,
                detail="one-hour forward observation acquisition started",
            )

    if request_pause_seconds:
        await asyncio.sleep(request_pause_seconds)

    try:
        by_mint = await jupiter.tokens_by_mint(tuple(item.mint for item in due))
        store.record_provider_health("jupiter_market", success=True)
    except ProviderFailure as exc:
        provider_failures.append(f"{exc.provider}:{exc.error_class}")
        store.record_provider_health("jupiter_market", success=False,
                                     error_class=exc.error_class)
        by_mint = {}
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        error_class = type(exc).__name__
        provider_failures.append(f"jupiter:{error_class}")
        store.record_provider_health("jupiter_market", success=False,
                                     error_class=error_class)
        by_mint = {}
    jupiter_received_at = now if fixed_clock else datetime.now(UTC)
    fallback_needed = []
    for item in due:
        token = by_mint.get(item.mint)
        if (token is None or _positive_market_value(token.get("usdPrice")) is None
                or _positive_market_value(token.get("liquidity")) is None):
            fallback_needed.append(item.mint)
    fallback_pairs: dict[str, dict[str, Any]] = {}
    fallback_received_at: datetime | None = None
    fallback_error: ProviderFailure | None = None
    if fallback_needed and price_fallback is not None:
        try:
            for offset in range(0, len(fallback_needed), 30):
                fallback_pairs.update(await price_fallback.tokens_by_mint(
                    fallback_needed[offset:offset + 30]
                ))
            fallback_received_at = now if fixed_clock else datetime.now(UTC)
            store.record_provider_health("dexscreener_market", success=True,
                                         attempted_at=fallback_received_at)
        except ProviderFailure as exc:
            fallback_error = exc
            store.record_provider_health("dexscreener_market", success=False,
                                         error_class=exc.error_class)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            fallback_error = ProviderFailure("dexscreener", "tokens_by_mint",
                                             type(exc).__name__)
            store.record_provider_health("dexscreener_market", success=False,
                                         error_class=type(exc).__name__)
    recorded = unavailable = failed = 0
    enrichment_used = 0

    for item in due:
        token = by_mint.get(item.mint)
        discovery = store.conn.execute(
            "SELECT discovery_json,first_pool_at FROM trench_launches WHERE mint=?",
            (item.mint,),
        ).fetchone()
        if token is None:
            discovery_data = json.loads(str(discovery[0])) if discovery else {}
            source_token = {
                "id": item.mint,
                "firstPool": {"createdAt": str(discovery[1])} if discovery else None,
                "discovery_snapshot": discovery_data,
            }
            # Discovery prices and flow are historical to discovery time. They are
            # evidence for identity only and cannot stand in for a due observation.
            token = {"id": item.mint,
                     "firstPool": {"createdAt": str(discovery[1])} if discovery else None}
        else:
            source_token = dict(token)
        token = dict(token)
        jupiter_price = _positive_market_value(token.get("usdPrice"))
        jupiter_liquidity = _positive_market_value(token.get("liquidity"))
        fallback_pair = fallback_pairs.get(item.mint)
        fallback_price = _positive_market_value(fallback_pair.get("priceUsd")) if fallback_pair else None
        fallback_liquidity_data = fallback_pair.get("liquidity") if fallback_pair else None
        fallback_liquidity = (_positive_market_value(fallback_liquidity_data.get("usd"))
                              if isinstance(fallback_liquidity_data, dict) else None)
        if jupiter_price is None and fallback_price is not None:
            token["usdPrice"] = fallback_price
        if jupiter_liquidity is None and fallback_liquidity is not None:
            token["liquidity"] = fallback_liquidity
            if fallback_pair is not None:
                token["_noema_market_data_fallback"] = fallback_pair
        if _positive_market_value(token.get("usdPrice")) is None:
            store.record_provider_health("jupiter_price", success=False,
                                         error_class="price_unavailable")
            detail = "price unavailable from Jupiter and independent DexScreener source"
            if fallback_error:
                detail += f": {fallback_error.error_class}"
            store.record_attempt(item, status="unavailable", detail=detail,
                                 raw=token,
                                 attempted_at=now if fixed_clock else datetime.now(UTC))
            unavailable += 1
            continue
        store.record_provider_health("jupiter_price", success=jupiter_price is not None,
                                     error_class=None if jupiter_price is not None else "price_unavailable")

        holder_shares: tuple[float, ...] = ()
        enrichment_detail: str | None = None
        enrichment_error_class: str | None = None
        if (
            item.horizon_seconds in ENRICHMENT_HORIZONS
            and enrichment_used < enrichment_limit
        ):
            enrichment_used += 1
            if hasattr(solana, "provider_cooldowns"):
                cooldowns: dict[str, datetime] = {}
                for alias, state in store.provider_states().items():
                    if not alias.startswith("solana_rpc:") or not state.get("next_retry_at"):
                        continue
                    try:
                        cooldowns[alias.split(":", 1)[1]] = datetime.fromisoformat(
                            str(state["next_retry_at"])
                        ).astimezone(UTC)
                    except (TypeError, ValueError):
                        continue
                solana.provider_cooldowns = cooldowns
            try:
                holder_shares = await solana.top_account_supply_shares(item.mint)
                rpc_attempt = getattr(solana, "last_attempt_provenance", None) or {}
                failed_providers = rpc_attempt.get("failed_providers", [])
                for provider_failure in failed_providers:
                    if isinstance(provider_failure, dict):
                        if provider_failure.get("error_class") == "backoff_active":
                            continue
                        store.record_provider_health(
                            f"solana_rpc:{provider_failure.get('source_alias', 'unknown')}",
                            success=False,
                            error_class=str(provider_failure.get("error_class", "provider_failure")),
                        )
                store.record_provider_health(
                    f"solana_rpc:{solana.last_provider or 'unknown'}", success=True,
                )
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
                for provider_failure in failed_providers:
                    if isinstance(provider_failure, dict):
                        if provider_failure.get("error_class") == "backoff_active":
                            continue
                        store.record_provider_health(
                            f"solana_rpc:{provider_failure.get('source_alias', 'unknown')}",
                            success=False,
                            error_class=str(provider_failure.get("error_class", "provider_failure")),
                        )
                if (not failed_providers
                        and error_class != "backoff_active"):
                    store.record_provider_health(
                        "solana_rpc:primary", success=False, error_class=error_class,
                    )
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
                if error_class != "backoff_active":
                    provider_failures.append(f"solana_rpc:{error_class}")

        try:
            used_fallback = (jupiter_price is None or jupiter_liquidity is None)
            effective_observed_at = max(
                [jupiter_received_at]
                + ([fallback_received_at] if used_fallback and fallback_received_at else [])
            )
            tick = jupiter_launch_tick(
                token,
                observed_at=effective_observed_at,
                holder_shares=holder_shares,
            )
            control = jupiter_control_state(token)
            price_source = "jupiter.usdPrice" if jupiter_price is not None else "dexscreener.priceUsd"
            liquidity_source = ("jupiter.liquidity" if jupiter_liquidity is not None
                                else "dexscreener.liquidity.usd")
            provenance = {
                "observation": {"provider": "jupiter" if jupiter_price is not None else "dexscreener",
                                "received_at": effective_observed_at.isoformat(),
                                "freshness_basis": "local_response_received_at",
                                "upstream_quote_timestamp": "unavailable"},
                "fields": {
                    "first_pool_at": "jupiter.firstPool",
                    "price_usd": price_source,
                    "liquidity_usd": liquidity_source,
                    "flow_and_participant_metrics": "jupiter.stats24h",
                    "token_controls": "jupiter.audit",
                    "holder_shares": (f"solana_rpc.{solana.last_provider}"
                                       if holder_shares else None),
                },
                "market_fallback": (
                    {"provider": "dexscreener", "received_at": fallback_received_at.isoformat(),
                     "pair_address": fallback_pair.get("pairAddress"),
                     "dex_id": fallback_pair.get("dexId"),
                     "price_usd": fallback_price, "liquidity_usd": fallback_liquidity,
                     "upstream_quote_timestamp": "unavailable",
                     "activity_window": "m5", "minimum_recent_trades": 1}
                    if fallback_pair is not None else None
                ),
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
                raw_token={
                    **source_token,
                    "_noema_market_data_fallback": fallback_pair,
                    "_noema_normalized_price_usd": tick.price_usd,
                    "_noema_normalized_liquidity_usd": tick.liquidity_usd,
                },
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
        provider_health=tuple(
            value
            for provider, state in sorted(store.provider_states().items())
            for value in (
                f"{provider}={state['state']}",
                (f"{provider}_details=last_attempt_at:{state['last_attempt_at']};"
                 f"last_success_at:{state['last_success_at']};"
                 f"last_error_class:{state['last_error_class']};"
                 f"consecutive_failures:{state['consecutive_failures']}"),
            )
        ),
    )


async def collect_trench_cycle(
    *,
    db_path: str,
    jupiter: JupiterTrenchResearchClient,
    solana: SolanaRpcResearchClient,
    price_fallback: DexScreenerTrenchPriceClient | None = None,
    now: datetime | None = None,
    due_limit: int = 20,
    enrichment_limit: int = 2,
    request_pause_seconds: float = 0.0,
    discover_new: bool = True,
) -> TrenchCollectionSummary:
    """Run one bounded collection cycle and close its persistent-store handle."""
    loop = asyncio.get_running_loop()
    lock = _COLLECTOR_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _COLLECTOR_LOCKS[loop] = lock
    async with lock:
        store = TrenchCollectorStore(db_path)
        try:
            return await _collect_trench_cycle(
                db_path=db_path, store=store, jupiter=jupiter, solana=solana,
                price_fallback=price_fallback, now=now,
                due_limit=due_limit, enrichment_limit=enrichment_limit,
                request_pause_seconds=request_pause_seconds, discover_new=discover_new,
            )
        finally:
            store.close()
