"""Operator-reported cash accounting for the recurring NOEMA bill.

Paper forecasts, internal economic snapshots and wallet balances never enter this ledger.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar

from .economic_ledger import EconomicEvent, ensure_economic_event_schema, record_event_on_connection


def _money(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0 or value.as_tuple().exponent < -2:
        raise ValueError("amount must be finite, non-negative USD with at most two decimals")
    return value


class BillTracker:
    HOSTED_BOOTSTRAP_ENV: ClassVar[dict[str, str]] = {
        "hosting_usd": "NOEMA_HOSTED_BILL_BUDGET_HOSTING_USD",
        "other_usd": "NOEMA_HOSTED_BILL_BUDGET_OTHER_USD",
        "model_budget_usd": "NOEMA_HOSTED_BILL_BUDGET_MODEL_USD",
        "owner_limit_usd": "NOEMA_HOSTED_BILL_BUDGET_OWNER_LIMIT_USD",
    }

    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(db)
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
                created_at TEXT NOT NULL,
                activity_id TEXT
            )"""
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(bill_entries)")}
        if "activity_id" not in columns:
            self.conn.execute("ALTER TABLE bill_entries ADD COLUMN activity_id TEXT")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS bill_entries_activity_id ON bill_entries(activity_id)"
        )
        ensure_economic_event_schema(self.conn)
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

    def bootstrap_hosted_budget_from_env(self) -> str:
        """Initialize only an absent budget from a complete owner-supplied config.

        The transaction lock and ``INSERT OR IGNORE`` make concurrent startup
        safe. Persisted operator edits always win; environment changes never
        update an existing budget row.
        """
        raw = {field: os.getenv(variable) for field, variable in self.HOSTED_BOOTSTRAP_ENV.items()}
        if any(value is None or not value.strip() for value in raw.values()):
            return "not_configured"
        try:
            amounts = {field: _money(Decimal(str(value))) for field, value in raw.items()}
        except (InvalidOperation, TypeError, ValueError):
            return "invalid_configuration"
        if amounts["model_budget_usd"] > amounts["other_usd"]:
            return "invalid_configuration"

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT 1 FROM bill_budget WHERE id=1"
            ).fetchone()
            if existing:
                self.conn.commit()
                return "persisted_budget_retained"
            prior_changes = self.conn.total_changes
            self.conn.execute(
                "INSERT OR IGNORE INTO bill_budget "
                "(id,hosting_usd,other_usd,model_budget_usd,owner_limit_usd,updated_at) "
                "VALUES (1,?,?,?,?,?)",
                (str(amounts["hosting_usd"]), str(amounts["other_usd"]),
                 str(amounts["model_budget_usd"]), str(amounts["owner_limit_usd"]),
                 datetime.now(UTC).isoformat()),
            )
            self.conn.commit()
            return ("initialized" if self.conn.total_changes > prior_changes
                    else "persisted_budget_retained")
        except Exception:
            self.conn.rollback()
            raise

    def record(
        self, *, kind: str, amount_usd: Decimal, source: str,
        reference: str, now: datetime | None = None, activity_id: str | None = None,
    ) -> None:
        if kind not in {"receipt", "expense"}:
            raise ValueError("kind must be receipt or expense")
        if _money(amount_usd) == 0:
            raise ValueError("entry amount must be positive")
        if not source.strip() or not reference.strip():
            raise ValueError("source and unique reference are required")
        if activity_id is not None and (not activity_id.strip() or len(activity_id) > 128):
            raise ValueError("activity_id must be a non-empty identifier of at most 128 characters")
        at = (now or datetime.now(UTC)).astimezone(UTC)
        created_at = at.isoformat()
        cursor = self.conn.execute(
            """INSERT INTO bill_entries
            (month_utc, kind, amount_usd, source, reference, created_at, activity_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (at.strftime("%Y-%m"), kind, str(amount_usd), source.strip(),
             reference.strip(), created_at, activity_id.strip() if activity_id else None),
        )
        event_type = "operator_cash_receipt" if kind == "receipt" else "operator_expense_report"
        record_event_on_connection(self.conn, EconomicEvent(
            provider="operator_bill", external_reference_id=reference.strip(),
            event_type=event_type, occurred_at=at, currency="USD", amount=amount_usd,
            amount_usd=amount_usd, reconciliation_state="OBSERVED", value_state="realized",
            capital_class="unclassified_cash" if kind == "receipt" else "cost",
            confidence_state="operator_reported", completeness_state="incomplete",
            activity_id=activity_id.strip() if activity_id else None,
            evidence={"bill_entry_id": int(cursor.lastrowid), "source": source.strip(),
                      "reference": reference.strip(), "classification": "operator_reported"},
        ))
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
