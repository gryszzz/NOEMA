from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


class WalletBudgetLedger:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_intent_budget (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                wallet_id TEXT NOT NULL,
                intent_id TEXT NOT NULL UNIQUE,
                notional_usd TEXT NOT NULL,
                status TEXT NOT NULL,
                mission_id TEXT
            )
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(wallet_intent_budget)")}
        if "mission_id" not in columns:
            self.conn.execute("ALTER TABLE wallet_intent_budget ADD COLUMN mission_id TEXT")
        self.conn.commit()

    def record(
        self,
        *,
        wallet_id: str,
        intent_id: str,
        notional_usd: Decimal,
        status: str,
        mission_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO wallet_intent_budget
            (created_at, wallet_id, intent_id, notional_usd, status, mission_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                wallet_id,
                intent_id,
                str(notional_usd),
                status,
                mission_id,
            ),
        )
        self.conn.commit()

    def has_intent(self, intent_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM wallet_intent_budget WHERE intent_id=?", (intent_id,),
        ).fetchone() is not None

    def reserve(
        self, *, wallet_id: str, intent_id: str, mission_id: str,
        notional_usd: Decimal, per_action_limit: Decimal,
        global_daily_limit: Decimal, mission_limit: Decimal,
        authority_daily_limit: Decimal, maximum_exposure: Decimal,
    ) -> str:
        """Consume an intent and enforce cumulative caps atomically."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.has_intent(intent_id):
                self.conn.rollback()
                return "replay"
            day_prefix = datetime.now(UTC).date().isoformat() + "%"
            daily_rows = self.conn.execute(
                "SELECT notional_usd FROM wallet_intent_budget WHERE wallet_id=? "
                "AND created_at LIKE ? AND status IN ('reserved','approved','submitted','confirmed','unknown','failed')",
                (wallet_id, day_prefix),
            ).fetchall()
            mission_rows = self.conn.execute(
                "SELECT notional_usd FROM wallet_intent_budget WHERE wallet_id=? AND mission_id=? "
                "AND status IN ('reserved','approved','submitted','confirmed','unknown','failed')",
                (wallet_id, mission_id),
            ).fetchall()
            daily_used = sum((Decimal(row[0]) for row in daily_rows), Decimal(0))
            mission_used = sum((Decimal(row[0]) for row in mission_rows), Decimal(0))
            allowed = (
                notional_usd > 0
                and notional_usd <= per_action_limit
                and daily_used + notional_usd <= min(global_daily_limit, authority_daily_limit)
                and mission_used + notional_usd <= min(mission_limit, maximum_exposure)
            )
            status = "reserved" if allowed else "rejected"
            self.conn.execute(
                "INSERT INTO wallet_intent_budget "
                "(created_at,wallet_id,intent_id,notional_usd,status,mission_id) VALUES (?,?,?,?,?,?)",
                (datetime.now(UTC).isoformat(), wallet_id, intent_id,
                 str(notional_usd), status, mission_id),
            )
            self.conn.commit()
            return status
        except BaseException:
            self.conn.rollback()
            raise

    def mission_exposure(
        self, wallet_id: str, mission_id: str, *, exclude_intent_id: str | None = None,
    ) -> Decimal:
        exclusion = " AND intent_id<>?" if exclude_intent_id else ""
        rows = self.conn.execute(
            "SELECT notional_usd FROM wallet_intent_budget "
            "WHERE wallet_id=? AND mission_id=? AND status IN ('reserved','approved','submitted','confirmed','unknown','failed')"
            + exclusion,
            ((wallet_id, mission_id, exclude_intent_id) if exclude_intent_id
             else (wallet_id, mission_id)),
        ).fetchall()
        return sum((Decimal(row[0]) for row in rows), Decimal(0))

    def daily_notional(
        self,
        wallet_id: str,
        *,
        day_prefix: str | None = None,
        exclude_intent_id: str | None = None,
    ) -> Decimal:
        prefix = day_prefix or datetime.now(UTC).date().isoformat()
        exclusion = " AND intent_id<>?" if exclude_intent_id else ""
        params = ((wallet_id, f"{prefix}%", exclude_intent_id) if exclude_intent_id
                  else (wallet_id, f"{prefix}%"))
        rows = self.conn.execute(
            "SELECT notional_usd FROM wallet_intent_budget WHERE wallet_id=? "
            "AND created_at LIKE ? AND status IN ('reserved','approved','submitted','confirmed','unknown','failed')"
            + exclusion,
            params,
        ).fetchall()
        return sum((Decimal(row[0]) for row in rows), Decimal(0))

    def update_status(self, intent_id: str, status: str) -> None:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE wallet_intent_budget SET status=? WHERE intent_id=?",
                (status, intent_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("wallet intent budget record does not exist")
