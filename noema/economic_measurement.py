"""Read-only economic measurement without mixing cash, estimates, and paper P&L."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .history_forecaster import MODEL_VERSION
from .paper_execution import parse_aware_time
from .paper_settlements import load_paper_settlements


def _amount(value: object) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid economic amount")
    return amount


def build_economic_measurement(
    path: str = "data/noema.db", *, now: datetime | None = None,
) -> dict[str, object]:
    """Current UTC month through `now`; does not create or migrate a database.

    Receipt/expense classification is operator-reported. There is no reconciliation
    or full-cost coverage attestation yet, so net economic profit stays unknown.
    Model reservations are exposure estimates, never additional booked expenses.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("measurement clock requires a timezone")
    now = now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    receipts = expenses = reserved = Decimal(0)
    recorded = invalid = reservations = 0
    estimate: Decimal | None = None
    present = Path(path).exists()
    if present:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            if "bill_entries" in tables:
                for kind, value, timestamp in conn.execute(
                    "SELECT kind, amount_usd, created_at FROM bill_entries"
                ):
                    try:
                        at = parse_aware_time(timestamp)
                        if not start <= at <= now:
                            continue
                        amount = _amount(value)
                        if kind not in {"receipt", "expense"} or amount == 0:
                            raise ValueError("invalid cash entry")
                    except (ValueError, TypeError, InvalidOperation):
                        invalid += 1
                        continue
                    recorded += 1
                    if kind == "receipt":
                        receipts += amount
                    else:
                        expenses += amount
            if "bill_budget" in tables:
                budget = conn.execute(
                    "SELECT hosting_usd, other_usd, updated_at FROM bill_budget WHERE id=1"
                ).fetchone()
                if budget:
                    try:
                        if parse_aware_time(budget[2]) <= now:
                            estimate = _amount(budget[0]) + _amount(budget[1])
                    except (ValueError, TypeError, InvalidOperation):
                        invalid += 1
            if "cognition_budget_reservations" in tables:
                for value, timestamp in conn.execute(
                    "SELECT estimated_usd, created_at FROM cognition_budget_reservations"
                ):
                    try:
                        if not start <= parse_aware_time(timestamp) <= now:
                            continue
                        amount = _amount(value)
                    except (ValueError, TypeError, InvalidOperation):
                        invalid += 1
                        continue
                    reserved += amount
                    reservations += 1
        finally:
            conn.close()

    paper = load_paper_settlements(path, now=now)
    monthly_paper = [item for item in paper.settlements if start <= item.observed_at <= now]
    net_cash = receipts - expenses
    return {
        "as_of": now.isoformat(), "month_utc": start.strftime("%Y-%m"),
        "database_present": present,
        "status": "records_invalid" if invalid or paper.invalid_quote_count else
                  "no_recorded_cash" if not recorded else
                  "recorded_cash_deficit" if net_cash < 0 else
                  "recorded_cash_surplus" if net_cash > 0 else "recorded_cash_balanced",
        "cash": {
            "basis": "operator-reported, unreconciled USD cash entries",
            "entry_count": recorded, "receipts_usd": str(receipts),
            "expenses_usd": str(expenses), "net_cash_usd": str(net_cash),
        },
        "operating_estimates": {
            "monthly_bill_usd": str(estimate) if estimate is not None else None,
            "model_reserved_usd": str(reserved), "model_attempt_count": reservations,
            "basis": "reservations include failed attempts; estimates are not invoices",
        },
        "paper": {
            "model_version": MODEL_VERSION,
            "settled_markets_this_month": len(monthly_paper),
            "settled_events_this_month": len({item.event_id for item in monthly_paper}),
            "net_after_execution_costs_usd": str(sum(
                (item.net_pnl_usd for item in monthly_paper), Decimal(0),
            )),
            "pending_markets": paper.pending_quote_count,
            "invalid_quotes": paper.invalid_quote_count,
            "duplicate_quotes": paper.duplicate_quote_count,
            "basis": "hypothetical fills; operating costs and capital opportunity cost excluded",
        },
        "invalid_accounting_records": invalid,
        "net_economic_profit_usd": None,
        "self_funding_demonstrated": False,
        "gaps": [
            "Cash classifications and completeness have not been reconciled to providers.",
            "Hosting, data, model and failed-experiment costs lack complete activity attribution.",
            "Capital usage, latency and opportunity costs are not fully measured.",
            "Paper returns and internal equity are excluded from cash and cannot establish revenue.",
        ],
    }
