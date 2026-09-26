"""Operator-reported cash accounting for the recurring NOEMA bill.

Paper forecasts, internal economic snapshots and wallet balances never enter this ledger.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def _money(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0 or value.as_tuple().exponent < -2:
        raise ValueError("amount must be finite, non-negative USD with at most two decimals")
    return value


class BillTracker:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bill_budget (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                hosting_usd TEXT NOT NULL,
                other_usd TEXT NOT NULL,
                model_budget_usd TEXT NOT NULL,
                owner_limit_usd TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bill_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month_utc TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('receipt', 'expense')),
                amount_usd TEXT NOT NULL,
                source TEXT NOT NULL,
                reference TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def configure(
        self, *, hosting_usd: Decimal, other_usd: Decimal, owner_limit_usd: Decimal,
        model_budget_usd: Decimal = Decimal(0),
    ) -> None:
        amounts = tuple(map(_money, (hosting_usd, other_usd, model_budget_usd,
                                     owner_limit_usd)))
        if model_budget_usd > other_usd:
            raise ValueError("model budget must fit within other monthly expenses")
        self.conn.execute(
            """INSERT INTO bill_budget VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET hosting_usd=excluded.hosting_usd,
                other_usd=excluded.other_usd, model_budget_usd=excluded.model_budget_usd,
                owner_limit_usd=excluded.owner_limit_usd,
                updated_at=excluded.updated_at""",
            (*map(str, amounts), datetime.now(UTC).isoformat()),
        )
        self.conn.commit()

    def record(
        self, *, kind: str, amount_usd: Decimal, source: str,
        reference: str, now: datetime | None = None,
    ) -> None:
        if kind not in {"receipt", "expense"}:
            raise ValueError("kind must be receipt or expense")
        if _money(amount_usd) == 0:
            raise ValueError("entry amount must be positive")
        if not source.strip() or not reference.strip():
            raise ValueError("source and unique reference are required")
        at = (now or datetime.now(UTC)).astimezone(UTC)
        self.conn.execute(
            """INSERT INTO bill_entries
            (month_utc, kind, amount_usd, source, reference, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (at.strftime("%Y-%m"), kind, str(amount_usd), source.strip(),
             reference.strip(), at.isoformat()),
        )
        self.conn.commit()

    def overview(self, *, now: datetime | None = None) -> dict[str, str | None]:
        month = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")
        budget = self.conn.execute(
            "SELECT hosting_usd, other_usd, model_budget_usd, owner_limit_usd "
            "FROM bill_budget WHERE id = 1"
        ).fetchone()
        entries = self.conn.execute(
            "SELECT kind, amount_usd FROM bill_entries WHERE month_utc = ?", (month,)
        ).fetchall()
        receipts = sum((Decimal(amount) for kind, amount in entries if kind == "receipt"),
                       Decimal(0))
        expenses = sum((Decimal(amount) for kind, amount in entries if kind == "expense"),
                       Decimal(0))
        result: dict[str, str | None] = {
            "month_utc": month,
            "basis": "operator-reported cash entries; paper results excluded",
            "hosting_estimate_usd": None,
            "other_estimate_usd": None,
            "model_budget_usd": None,
            "owner_limit_usd": None,
            "estimated_monthly_bill_usd": None,
            "recorded_receipts_usd": str(receipts),
            "recorded_expenses_usd": str(expenses),
            "uncovered_estimate_usd": None,
            "status": "estimate_missing",
        }
        if budget is None:
            return result
        host, other, model_budget, limit = map(Decimal, budget)
        target = host + other
        # Actual entries above the estimate increase exposure; an unpaid estimated bill
        # still counts. Receipts are operator-reported, never inferred from paper P&L.
        uncovered = max(Decimal(0), max(target, expenses) - receipts)
        result.update({
            "hosting_estimate_usd": str(host),
            "other_estimate_usd": str(other),
            "model_budget_usd": str(model_budget),
            "owner_limit_usd": str(limit),
            "estimated_monthly_bill_usd": str(target),
            "uncovered_estimate_usd": str(uncovered),
            "status": (
                "over_owner_limit" if uncovered > limit else
                "covered_by_reported_receipts" if uncovered == 0 else "within_owner_limit"
            ),
        })
        return result
