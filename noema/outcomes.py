from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .evaluation import expected_calibration_error, score_forecast


@dataclass(frozen=True)
class EvaluationSummary:
    count: int
    mean_brier: float
    mean_log_loss: float
    calibration_error: float


class OutcomeStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                venue TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_yes INTEGER NOT NULL CHECK (outcome_yes IN (0, 1)),
                resolved_at TEXT,
                first_seen_at TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                canonical_proposition_id TEXT,
                source TEXT,
                source_url TEXT,
                PRIMARY KEY (venue, market_id)
            )
            """
        )
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS outcome_scan_state (
                source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT NOT NULL
            )
        """)
        if "first_seen_at" not in {
            row[1] for row in self.conn.execute("PRAGMA table_info(outcomes)")
        }:
            # Backfilled legacy rows were not demonstrably available earlier.
            self.conn.execute("ALTER TABLE outcomes ADD COLUMN first_seen_at TEXT")
            self.conn.execute(
                "UPDATE outcomes SET first_seen_at = ? WHERE first_seen_at IS NULL",
                (datetime.now(UTC).isoformat(),),
            )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(outcomes)")}
        for name in ("canonical_proposition_id", "source", "source_url"):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE outcomes ADD COLUMN {name} TEXT")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS outcome_observations (
                evidence_hash TEXT PRIMARY KEY,
                venue TEXT NOT NULL,
                market_id TEXT NOT NULL,
                canonical_proposition_id TEXT,
                outcome_yes INTEGER NOT NULL CHECK (outcome_yes IN (0, 1)),
                observed_at TEXT NOT NULL,
                resolved_at TEXT,
                source TEXT NOT NULL,
                source_url TEXT,
                raw_json TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def scan_cursor(self, source: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM outcome_scan_state WHERE source = ?", (source,)
        ).fetchone()
        return row[0] if row else None

    def set_scan_cursor(self, source: str, cursor: str | None) -> None:
        self.conn.execute("""
            INSERT INTO outcome_scan_state (source, cursor, updated_at)
            VALUES (?, ?, ?) ON CONFLICT(source) DO UPDATE SET
                cursor = excluded.cursor, updated_at = excluded.updated_at
        """, (source, cursor, datetime.now(UTC).isoformat()))
        self.conn.commit()

    def pending_paper_quotes(
        self, venue: str, *, limit: int,
    ) -> list[tuple[int, str]]:
        """Round-robin unresolved paper positions before broad settlement scans."""
        if limit <= 0:
            return []
        source = f"{venue}:settlements:pending_id"
        try:
            last_id = int(self.scan_cursor(source) or "0")
        except ValueError:
            last_id = 0
        sql = """
            SELECT q.id, q.market_id FROM paper_quotes q
            WHERE q.venue = ? AND q.selected = 1 AND q.id {comparison} ?
              AND NOT EXISTS (
                SELECT 1 FROM outcomes o
                WHERE o.venue = q.venue AND o.market_id = q.market_id
              )
            ORDER BY q.id LIMIT ?
        """
        try:
            rows = self.conn.execute(sql.format(comparison=">"), (venue, last_id, limit)).fetchall()
            if len(rows) < limit:
                rows += self.conn.execute(
                    sql.format(comparison="<="), (venue, last_id, limit - len(rows)),
                ).fetchall()
        except sqlite3.OperationalError:
            return []  # Older databases have no paper quote table yet.
        return rows

    def upsert(
        self,
        *,
        venue: str,
        market_id: str,
        outcome_yes: int,
        resolved_at: str | None,
        raw: dict[str, object],
        seen_at: datetime | None = None,
        canonical_proposition_id: str | None = None,
        source: str | None = None,
        source_url: str | None = None,
    ) -> None:
        if outcome_yes not in {0, 1}:
            raise ValueError("outcome_yes must be 0 or 1")
        seen_at = seen_at or datetime.now(UTC)
        if seen_at.tzinfo is None:
            raise ValueError("seen_at must be timezone-aware")
        raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        observed_at = seen_at.astimezone(UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO outcomes
            (venue, market_id, outcome_yes, resolved_at, first_seen_at, raw_json,
             canonical_proposition_id, source, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue, market_id) DO UPDATE SET
              outcome_yes=excluded.outcome_yes,
              resolved_at=excluded.resolved_at,
              first_seen_at=CASE WHEN outcomes.outcome_yes != excluded.outcome_yes
                  THEN excluded.first_seen_at ELSE outcomes.first_seen_at END,
              raw_json=excluded.raw_json,
              canonical_proposition_id=COALESCE(excluded.canonical_proposition_id,
                  outcomes.canonical_proposition_id),
              source=COALESCE(excluded.source, outcomes.source),
              source_url=COALESCE(excluded.source_url, outcomes.source_url)
            """,
            (
                venue, market_id, outcome_yes, resolved_at,
                observed_at, raw_json, canonical_proposition_id, source, source_url,
            ),
        )
        evidence_identity = json.dumps(
            [venue, market_id, outcome_yes, resolved_at, source, source_url, raw],
            sort_keys=True, separators=(",", ":"), default=str,
        )
        digest = hashlib.sha256(evidence_identity.encode()).hexdigest()
        self.conn.execute(
            """INSERT OR IGNORE INTO outcome_observations
               (evidence_hash,venue,market_id,canonical_proposition_id,outcome_yes,
                observed_at,resolved_at,source,source_url,raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (digest, venue, market_id, canonical_proposition_id, outcome_yes,
             observed_at, resolved_at, source or "unspecified", source_url, raw_json),
        )
        self.conn.commit()

    def evaluate_ledger(self) -> EvaluationSummary:
        try:
            rows = self.conn.execute(
                """
                SELECT f.venue, f.market_id, f.snapshot_json, f.forecast_json,
                       o.outcome_yes, o.resolved_at
                FROM forecast_ledger f
                JOIN outcomes o
                  ON o.venue = f.venue AND o.market_id = f.market_id
                ORDER BY f.id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

        if not rows:
            return EvaluationSummary(0, 0.0, 0.0, 0.0)

        probs_and_outcomes: list[tuple[float, int]] = []
        briers: list[float] = []
        log_losses: list[float] = []

        seen: set[tuple[str, str, str]] = set()
        for venue, ticker, snapshot_json, forecast_json, outcome_yes, resolved_at in rows:
            forecast = json.loads(forecast_json)
            snapshot = json.loads(snapshot_json)
            if not resolved_at:
                continue
            try:
                capture = datetime.fromisoformat(snapshot["captured_at"])
                resolved = datetime.fromisoformat(resolved_at)
            except (KeyError, ValueError):
                continue
            if capture.tzinfo is None or resolved.tzinfo is None or capture >= resolved:
                continue
            key = (venue, ticker, str(forecast.get("model_version", "unknown")))
            if key in seen:
                continue
            seen.add(key)
            probability = float(forecast["probability_yes"])
            outcome = int(outcome_yes)
            scored = score_forecast(probability, outcome)
            probs_and_outcomes.append((probability, outcome))
            briers.append(scored.brier)
            log_losses.append(scored.log_loss)

        count = len(briers)
        if not count:
            return EvaluationSummary(0, 0.0, 0.0, 0.0)
        return EvaluationSummary(
            count=count,
            mean_brier=sum(briers) / count,
            mean_log_loss=sum(log_losses) / count,
            calibration_error=expected_calibration_error(probs_and_outcomes),
        )
