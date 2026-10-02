from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .cognition_models import CognitionPacket
from .economic_ledger import EconomicEvent, ensure_economic_event_schema, record_event_on_connection


def _next_month(day: str) -> str:
    year, month = map(int, day[:7].split("-"))
    return f"{year + (month == 12):04d}-{(month % 12) + 1:02d}-01"


class CognitionStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cognition_packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                market_id TEXT NOT NULL,
                deployment TEXT NOT NULL,
                response_id TEXT,
                packet_json TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cognition_budget_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_utc TEXT NOT NULL,
                estimated_usd REAL NOT NULL CHECK (estimated_usd > 0),
                created_at TEXT NOT NULL,
                estimated_tokens INTEGER,
                market_id TEXT,
                activity_id TEXT
            )
            """
        )
        self.conn.commit()
        with self.conn:
            packet_columns = {
                row[1] for row in self.conn.execute("PRAGMA table_info(cognition_packets)")
            }
            if "decision_id" not in packet_columns:
                self.conn.execute("ALTER TABLE cognition_packets ADD COLUMN decision_id TEXT")
            if "trace_id" not in packet_columns:
                self.conn.execute("ALTER TABLE cognition_packets ADD COLUMN trace_id TEXT")
            if "trace_status" not in packet_columns:
                self.conn.execute("ALTER TABLE cognition_packets ADD COLUMN trace_status TEXT")
        # Existing reservations remain unknown, never silently treated as zero
        # token usage. Serialize migration against other worker connections.
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in self.conn.execute(
                    "PRAGMA table_info(cognition_budget_reservations)"
                )
            }
            if "estimated_tokens" not in columns:
                self.conn.execute(
                    "ALTER TABLE cognition_budget_reservations ADD COLUMN estimated_tokens INTEGER"
                )
            if "market_id" not in columns:
                self.conn.execute(
                    "ALTER TABLE cognition_budget_reservations ADD COLUMN market_id TEXT"
                )
            if "activity_id" not in columns:
                self.conn.execute(
                    "ALTER TABLE cognition_budget_reservations ADD COLUMN activity_id TEXT"
                )
            if "provider" not in columns:
                self.conn.execute(
                    "ALTER TABLE cognition_budget_reservations ADD COLUMN provider TEXT"
                )
            if "model" not in columns:
                self.conn.execute(
                    "ALTER TABLE cognition_budget_reservations ADD COLUMN model TEXT"
                )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS cognition_reservations_activity_id "
                "ON cognition_budget_reservations(activity_id)"
            )
        ensure_economic_event_schema(self.conn)

    def reserve_estimated_cost(
        self, cost_usd: float, *, daily_limit_usd: float,
        hourly_call_limit: int = 6,
        monthly_limit_usd: float | None = None,
        estimated_tokens: int | None = None,
        hourly_token_limit: int | None = None,
        market_id: str | None = None,
        activity_id: str | None = None,
        cooldown_seconds: float = 0,
        provider: str = "cognition",
        model: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Reserve call, token and cost estimates together, including failed attempts.

        Reservations are conservative spending bounds, not provider invoices.
        """
        if (
            not math.isfinite(cost_usd) or not math.isfinite(daily_limit_usd)
            or cost_usd <= 0 or daily_limit_usd <= 0
            or type(hourly_call_limit) is not int or hourly_call_limit <= 0
            or (monthly_limit_usd is not None and
                (not math.isfinite(monthly_limit_usd) or monthly_limit_usd <= 0))
        ):
            raise ValueError("budget amounts must be positive and finite")
        if (estimated_tokens is None) != (hourly_token_limit is None):
            raise ValueError("token estimate and hourly token limit must be supplied together")
        if estimated_tokens is not None and (
            type(estimated_tokens) is not int or estimated_tokens <= 0
            or type(hourly_token_limit) is not int or hourly_token_limit <= 0
        ):
            raise ValueError("token budgets must be positive integers")
        if (not math.isfinite(cooldown_seconds) or cooldown_seconds < 0
                or (cooldown_seconds > 0 and not market_id)):
            raise ValueError("cooldown must be non-negative and associated with a market")
        if activity_id is not None and (
            not isinstance(activity_id, str) or not activity_id.strip() or len(activity_id) > 128
        ):
            raise ValueError("activity_id must be a non-empty identifier of at most 128 characters")
        now = _utc_time(now)
        day = now.date().isoformat()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            recent = self.conn.execute(
                "SELECT estimated_tokens FROM cognition_budget_reservations "
                "WHERE julianday(created_at) > julianday(?) AND julianday(created_at) <= julianday(?)",
                ((now - timedelta(hours=1)).isoformat(), now.isoformat()),
            ).fetchall()
            if len(recent) >= hourly_call_limit:
                self.conn.rollback()
                return False
            if estimated_tokens is not None and (
                any(row[0] is None for row in recent)
                or sum(row[0] for row in recent) + estimated_tokens > hourly_token_limit
            ):
                self.conn.rollback()
                return False
            if market_id and cooldown_seconds > 0:
                previous = self.conn.execute(
                    "SELECT 1 FROM cognition_budget_reservations WHERE market_id = ? "
                    "AND julianday(created_at) > julianday(?) LIMIT 1",
                    (market_id, (now - timedelta(seconds=cooldown_seconds)).isoformat()),
                ).fetchone()
                if previous is not None:
                    self.conn.rollback()
                    return False
            spent = sum((Decimal(str(row[0])) for row in self.conn.execute(
                "SELECT estimated_usd "
                "FROM cognition_budget_reservations WHERE day_utc = ?", (day,),
            )), Decimal(0))
            if spent + Decimal(str(cost_usd)) > Decimal(str(daily_limit_usd)):
                self.conn.rollback()
                return False
            if monthly_limit_usd is not None:
                monthly_spent = sum((Decimal(str(row[0])) for row in self.conn.execute(
                    "SELECT estimated_usd "
                    "FROM cognition_budget_reservations WHERE day_utc >= ? AND day_utc < ?",
                    (day[:7] + "-01", _next_month(day)),
                )), Decimal(0))
                if monthly_spent + Decimal(str(cost_usd)) > Decimal(str(monthly_limit_usd)):
                    self.conn.rollback()
                    return False
            self.conn.execute(
                "INSERT INTO cognition_budget_reservations "
                "(day_utc, estimated_usd, created_at, estimated_tokens, market_id, activity_id,"
                "provider,model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (day, cost_usd, now.isoformat(), estimated_tokens, market_id, activity_id,
                 provider, model),
            )
            reservation_id = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            reservation = EconomicEvent(
                provider=provider, external_reference_id=f"cognition-reservation:{reservation_id}",
                event_type="model_cost_reservation", occurred_at=now, currency="USD",
                amount=Decimal(str(cost_usd)), amount_usd=Decimal(str(cost_usd)),
                reconciliation_state="ESTIMATED", value_state="reservation",
                capital_class="cost", confidence_state="estimated", completeness_state="incomplete",
                activity_id=activity_id, lane="cognition",
                evidence={"reservation_id": reservation_id, "model": model,
                          "estimated_tokens": estimated_tokens, "market_id": market_id},
            )
            record_event_on_connection(self.conn, reservation)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def append(
        self,
        *,
        deployment: str,
        packet: CognitionPacket,
        response_id: str | None,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        decision_id: str | None = None,
        trace_id: str | None = None,
        trace_status: str | None = None,
    ) -> None:
        if (any(type(value) is not int or value < 0
                for value in (input_tokens, output_tokens, total_tokens))
                or total_tokens < input_tokens + output_tokens):
            raise ValueError("model usage must contain consistent non-negative token counts")
        self.conn.execute(
            """
            INSERT INTO cognition_packets
            (created_at, market_id, deployment, response_id, packet_json,
             input_tokens, output_tokens, total_tokens, decision_id, trace_id, trace_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                packet.market_id,
                deployment,
                response_id,
                json.dumps(asdict(packet), sort_keys=True),
                input_tokens,
                output_tokens,
                total_tokens,
                decision_id,
                trace_id,
                trace_status,
            ),
        )
        self.conn.commit()

    def calls_since(self, since: datetime) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM cognition_packets
            WHERE created_at >= ?
            """,
            (since.astimezone(UTC).isoformat(),),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def calls_last_hour(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return self.calls_since(now - timedelta(hours=1))

    def tokens_since(self, since: datetime) -> int:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0)
            FROM cognition_packets
            WHERE created_at >= ?
            """,
            (since.astimezone(UTC).isoformat(),),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def tokens_last_hour(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return self.tokens_since(now - timedelta(hours=1))

    def monthly_model_budget_remaining(
        self, monthly_budget_usd: Decimal, *, now: datetime | None = None,
    ) -> Decimal:
        """Return remaining model budget after conservative monthly reservations."""
        if not monthly_budget_usd.is_finite() or monthly_budget_usd < 0:
            raise ValueError("monthly model budget must be finite and non-negative")
        now = _utc_time(now)
        start = now.strftime("%Y-%m-01")
        end = _next_month(start)
        rows = self.conn.execute(
            "SELECT estimated_usd FROM cognition_budget_reservations "
            "WHERE day_utc >= ? AND day_utc < ?", (start, end),
        ).fetchall()
        reserved = sum((Decimal(str(row[0])) for row in rows), Decimal(0))
        return max(Decimal(0), monthly_budget_usd - reserved)

    def seconds_since_market_call(
        self,
        market_id: str,
        *,
        now: datetime | None = None,
    ) -> float | None:
        row = self.conn.execute(
            """
            SELECT created_at
            FROM cognition_packets
            WHERE market_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()
        if row is None:
            return None
        now = now or datetime.now(UTC)
        created = datetime.fromisoformat(row[0])
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return max(0.0, (now - created.astimezone(UTC)).total_seconds())

    def latest(self) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT created_at, market_id, deployment, packet_json,
                   input_tokens, output_tokens, total_tokens, decision_id, trace_id,
                   trace_status
            FROM cognition_packets
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "created_at": row[0],
            "market_id": row[1],
            "deployment": row[2],
            "packet": json.loads(row[3]),
            "input_tokens": row[4],
            "output_tokens": row[5],
            "total_tokens": row[6],
            "decision_id": row[7],
            "trace_id": row[8],
            "trace_status": row[9],
        }


def _utc_time(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("budget timestamps must include a timezone")
    return now.astimezone(UTC)
