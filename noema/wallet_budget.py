from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


class WalletBudgetLedger:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_intent_budget (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                wallet_id TEXT NOT NULL,
                intent_id TEXT NOT NULL UNIQUE,
                notional_usd TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def record(
        self,
        *,
        wallet_id: str,
        intent_id: str,
        notional_usd: Decimal,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO wallet_intent_budget
            (created_at, wallet_id, intent_id, notional_usd, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                wallet_id,
                intent_id,
                str(notional_usd),
                status,
            ),
        )
        self.conn.commit()

    def daily_notional(
        self,
        wallet_id: str,
        *,
        day_prefix: str | None = None,
    ) -> Decimal:
        prefix = day_prefix or datetime.now(UTC).date().isoformat()
        rows = self.conn.execute(
            """
            SELECT notional_usd
            FROM wallet_intent_budget
            WHERE wallet_id = ?
              AND created_at LIKE ?
              AND status IN ('approved', 'submitted', 'confirmed')
            """,
            (wallet_id, f"{prefix}%"),
        ).fetchall()
        return sum((Decimal(row[0]) for row in rows), Decimal(0))
