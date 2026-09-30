"""Read-only economic measurement without mixing cash, estimates, and paper P&L."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .economic_investigations import latest_current_period_investigation
from .economic_ledger import EconomicLedger
from .history_forecaster import MODEL_VERSION
from .paper_execution import parse_aware_time
from .paper_settlements import load_paper_settlements
from .research_state import merge_research_run_records, research_run_identity


def _amount(value: object) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid economic amount")
    return amount


def build_economic_measurement(
    path: str = "data/noema.db", *, additional_paths: tuple[str, ...] = (),
    now: datetime | None = None,
) -> dict[str, object]:
    """Current UTC month through `now`; does not create or migrate a database.

    Receipt/expense classification is operator-reported. Without reconciled events
    and complete provider-period coverage attestations, net economic profit stays unknown.
    Model reservations are exposure estimates, never additional booked expenses.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("measurement clock requires a timezone")
    now = now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    receipts = expenses = reserved = Decimal(0)
    recorded = invalid = reservations = 0
    activity_cash: dict[str, dict[str, Decimal | int]] = {}
    activity_costs: dict[str, dict[str, Decimal | int]] = {}
    estimate: Decimal | None = None
    present = Path(path).exists()
    if present or any(Path(source).is_file() for source in additional_paths):
        conn = (sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
                if present else sqlite3.connect(":memory:"))
        try:
            conn.execute("BEGIN")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            session_activity_ids: dict[str, str] = {}
            if "cognitive_sessions" in tables:
                session_columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(cognitive_sessions)"
                )}
                if {"session_id", "result_json"} <= session_columns:
                    for session_id, result_json in conn.execute(
                        "SELECT session_id,result_json FROM cognitive_sessions"
                    ):
                        activity_id = str(session_id)
                        if result_json:
                            try:
                                payload = json.loads(result_json)
                                if isinstance(payload, dict):
                                    activity_id = str(payload.get("trial_id") or activity_id)
                            except (ValueError, TypeError):
                                invalid += 1
                        session_activity_ids[str(session_id)] = activity_id
            if "bill_entries" in tables:
                bill_columns = {row[1] for row in conn.execute("PRAGMA table_info(bill_entries)")}
                activity_column = ", activity_id" if "activity_id" in bill_columns else ", NULL"
                for kind, value, timestamp, activity_id in conn.execute(
                    "SELECT kind, amount_usd, created_at" + activity_column + " FROM bill_entries"
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
                    if activity_id:
                        totals = activity_cash.setdefault(str(activity_id), {
                            "receipts": Decimal(0), "expenses": Decimal(0), "entries": 0,
                        })
                        totals["receipts" if kind == "receipt" else "expenses"] += amount
                        totals["entries"] += 1
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
                reservation_columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(cognition_budget_reservations)"
                )}
                activity_column = ", activity_id" if "activity_id" in reservation_columns else ", NULL"
                for value, timestamp, activity_id in conn.execute(
                    "SELECT estimated_usd, created_at" + activity_column
                    + " FROM cognition_budget_reservations"
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
                    if activity_id:
                        activity_id = session_activity_ids.get(str(activity_id), str(activity_id))
                        totals = activity_costs.setdefault(str(activity_id), {
                            "model_cost_estimate": Decimal(0), "model_budget_reserved": Decimal(0),
                            "compute_cost": Decimal(0), "priced_compute_entries": 0,
                            "reservation_entries": 0, "model_cost_entries": 0,
                        })
                        totals["model_budget_reserved"] += amount
                        totals["reservation_entries"] += 1

            def add_activity_cost(activity_id: str, key: str, value: object) -> None:
                totals = activity_costs.setdefault(activity_id, {
                    "model_cost_estimate": Decimal(0), "model_budget_reserved": Decimal(0),
                    "compute_cost": Decimal(0), "priced_compute_entries": 0,
                    "reservation_entries": 0, "model_cost_entries": 0,
                })
                if value is None:
                    return
                try:
                    amount = _amount(value)
                except (ValueError, TypeError, InvalidOperation):
                    nonlocal invalid
                    invalid += 1
                    return
                totals[key] += amount
                if key == "compute_cost":
                    totals["priced_compute_entries"] += 1
                elif key == "model_cost_estimate":
                    totals["model_cost_entries"] += 1

            if "cognitive_sessions" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(cognitive_sessions)")}
                required = {"session_id", "created_at", "result_json", "estimated_model_cost_usd",
                            "compute_cost_usd"}
                if required <= columns:
                    for session_id, timestamp, result_json, model_cost, compute_cost in conn.execute(
                        "SELECT session_id,created_at,result_json,estimated_model_cost_usd,"
                        "compute_cost_usd FROM cognitive_sessions"
                    ):
                        try:
                            if not start <= parse_aware_time(timestamp) <= now:
                                continue
                        except (ValueError, TypeError):
                            invalid += 1
                            continue
                        activity_id = str(session_id)
                        if result_json:
                            try:
                                payload = json.loads(result_json)
                                if isinstance(payload, dict):
                                    activity_id = str(payload.get("trial_id") or activity_id)
                            except (ValueError, TypeError):
                                invalid += 1
                        add_activity_cost(activity_id, "model_cost_estimate", model_cost)
                        add_activity_cost(activity_id, "compute_cost", compute_cost)
            research_runs: dict[tuple[str, str, str, str], dict[str, object]] = {}

            def collect_research_runs(run_conn: sqlite3.Connection) -> None:
                run_tables = {row[0] for row in run_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "autonomous_research_runs" not in run_tables:
                    return
                run_columns = {row[1] for row in run_conn.execute(
                    "PRAGMA table_info(autonomous_research_runs)")}
                if not {"trial_id", "created_at", "compute_cost_usd"} <= run_columns:
                    return
                selected = [name for name in (
                    "trial_id", "evidence_hash", "worker_version", "status", "created_at",
                    "completed_at", "updated_at", "compute_cost_usd", "elapsed_seconds",
                    "result_json", "evidence_path",
                ) if name in run_columns]
                for row in run_conn.execute(
                    f"SELECT {','.join(selected)} FROM autonomous_research_runs"):
                    item = dict(zip(selected, row, strict=True))
                    key = research_run_identity(item)
                    current = research_runs.get(key)
                    if current is None:
                        research_runs[key] = item
                    else:
                        research_runs[key] = merge_research_run_records(current, item)

            collect_research_runs(conn)
            for additional_path in additional_paths:
                additional_db = Path(additional_path)
                if not additional_db.is_file() or additional_db.resolve() == Path(path).resolve():
                    continue
                try:
                    with closing(sqlite3.connect(
                        additional_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2,
                    )) as additional:
                        collect_research_runs(additional)
                except sqlite3.Error:
                    continue
            for item in research_runs.values():
                try:
                    if not start <= parse_aware_time(item["created_at"]) <= now:
                        continue
                except (ValueError, TypeError):
                    invalid += 1
                    continue
                add_activity_cost(str(item["trial_id"]), "compute_cost", item["compute_cost_usd"])
        finally:
            conn.close()

    paper = load_paper_settlements(path, now=now)
    monthly_paper = [item for item in paper.settlements if start <= item.observed_at <= now]
    paper_pnl = Decimal(0)
    paper_pnl_series = []
    series_stride = max(1, (len(monthly_paper) + 179) // 180)
    for index, item in enumerate(monthly_paper):
        paper_pnl += item.net_pnl_usd
        if index % series_stride == 0 or index == len(monthly_paper) - 1:
            paper_pnl_series.append({
                "observed_at": item.observed_at.isoformat(),
                "venue": item.venue,
                "market_id": item.market_id,
                "realized_net_usd": str(item.net_pnl_usd),
                "cumulative_net_usd": str(paper_pnl),
            })
    canonical = EconomicLedger.read_projection(
        path, month_utc=start.strftime("%Y-%m"), additional_paths=additional_paths,
    )
    canonical["investigation_decision"] = latest_current_period_investigation(path, now=now)
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
        "activity_cash": {
            "basis": "recorded cash entries linked to activity_id; excludes estimates and paper P&L",
            "attribution_complete": False,
            "rows": [
                {
                    "activity_id": activity_id,
                    "recorded_receipts_usd": str(values["receipts"]),
                    "recorded_expenses_usd": str(values["expenses"]),
                    "recorded_cash_net_usd": str(values["receipts"] - values["expenses"]),
                    "entry_count": values["entries"],
                    "full_net_economic_profit_usd": None,
                    "status": "cash recorded; complete cost coverage not attested",
                }
                for activity_id, values in sorted(activity_cash.items())
            ],
        },
        "activity_costs": {
            "basis": "provider usage-price estimates and configured cost entries; reservations are ceilings, not invoices",
            "rows": [
                {
                    "activity_id": activity_id,
                    "estimated_model_cost_usd": (str(values["model_cost_estimate"])
                                                   if values["model_cost_entries"] else None),
                    "model_budget_reserved_usd": (str(values["model_budget_reserved"])
                                                   if values["reservation_entries"] else None),
                    "recorded_compute_cost_usd": str(values["compute_cost"])
                    if values["priced_compute_entries"] else None,
                    "compute_cost_status": "recorded" if values["priced_compute_entries"] else "unknown",
                    "full_net_economic_profit_usd": None,
                }
                for activity_id, values in sorted(activity_costs.items())
            ],
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
            "net_after_execution_costs_usd": str(paper_pnl),
            "pnl_series": paper_pnl_series,
            "pending_markets": paper.pending_quote_count,
            "invalid_quotes": paper.invalid_quote_count,
            "duplicate_quotes": paper.duplicate_quote_count,
            "basis": "hypothetical fills; operating costs and capital opportunity cost excluded",
        },
        "canonical_ledger": canonical,
        "mission_progress": {
            "optimization_target": "verified realized economic value after attributable costs and risk",
            "status": "verified" if canonical.get("self_funding_ratio") is not None else "unproven",
            "verified_realized_revenue_usd": canonical.get("verified_realized_revenue_usd"),
            "complete_attributable_costs_usd": canonical.get("verified_attributable_costs_usd"),
            "verified_cost_adjusted_value_usd": canonical.get("net_verified_contribution_usd"),
            "reserve_balance_usd": canonical.get("operating_reserve_usd"),
            "self_funding_ratio": canonical.get("self_funding_ratio"),
            "known_operator_reported_cash_receipts_usd": str(receipts),
            "known_operator_reported_cash_expenses_usd": str(expenses),
            "monthly_operating_estimate_usd": str(estimate) if estimate is not None else None,
            "owner_funded_reconciled_subtotal_usd": canonical.get("owner_funded_subtotal_usd"),
            "unreconciled_amount_usd": canonical.get("unreconciled_amount_usd"),
            "unknowns": canonical.get("unknowns", []),
            "basis": (
                "reconciled provider revenue, complete attributable costs, and audited reserves "
                "are not yet available; reported cash, estimates, and paper outcomes stay separate"
            ),
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
