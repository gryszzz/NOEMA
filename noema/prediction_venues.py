"""Read-only live health and public market data for prediction venues."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from polymarket_us.errors import PolymarketUSError

from .account import KalshiAccount
from .account_capital import kalshi_cash_usd, kalshi_observed_capital
from .canonical_comparability import (
    PROPOSITION_FAMILIES,
    EconomicQuote,
    SettlementTerms,
    append_shared_economic_event,
    assess_economic_comparability,
    assess_settlement_equivalence,
    canonical_observation_event,
    identify_tesla_q3_deliveries_threshold,
)
from .canonical_market_identity import (
    compare_contract_identities,
    identify_mlb_world_series_champion,
    registered_resolvers,
)
from .config import KalshiConfig, kalshi_production_read_only_config
from .console_state import console_state_db_path
from .cross_venue_experiment import (
    evaluate_candidate,
    extract_clause_evidence,
    mature_paper_pairs,
    persist_evaluation,
    recent_evaluations,
)
from .ledger import ForecastLedger
from .polymarket_account_stream import polymarket_stream_health
from .prediction_account_history import (
    get_or_create_prediction_account_baseline,
    load_prediction_account_records,
    persist_prediction_account_records,
    prediction_account_sync_state,
)
from .venues.kalshi import KalshiVenue
from .venues.polymarket_us import PolymarketUSVenue
from .wallet_credentials import (
    KALSHI_KEY_ID_KEYCHAIN_SERVICE,
    credential_source,
    load_polymarket_us_credentials_in_api_boundary,
    polymarket_us_credential_sources,
    polymarket_us_credentials_present,
    private_key_file_status,
)

_cache: dict[str, Any] = {"at": 0.0, "payload": None}
_registered_quote_pair: list[dict[str, Any]] | None = None
_registered_identity_review_at = 0.0
_lock = asyncio.Lock()
_HISTORY_AUDIT_SECONDS = 6 * 60 * 60
_OVERLAP_SECONDS = 300


def _account_history_plan(
    venue: str, record_types: tuple[str, ...], *, required_streams: tuple[str, ...] = ("activity", "settlement"),
) -> tuple[bool, int | None, dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Choose a durable incremental cursor and scheduled full-coverage audit."""
    path = console_state_db_path()
    state = prediction_account_sync_state(path, venue)
    now = datetime.now(UTC)
    full_audit = True
    for stream in required_streams:
        row = state.get(stream) or {}
        try:
            last = datetime.fromisoformat(str(row.get("last_full_audit_at")))
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last.astimezone(UTC)).total_seconds() < _HISTORY_AUDIT_SECONDS:
                continue
        except (TypeError, ValueError):
            pass
        full_audit = True
        break
    else:
        full_audit = False
    records = load_prediction_account_records(path, venue, record_types=record_types)
    latest: datetime | None = None
    for rows in records.values():
        for row in rows:
            raw = row.get("created_time") or row.get("created_at") or row.get("settled_time")
            try:
                value = datetime.fromisoformat(str(raw))
                value = value.astimezone(UTC)
            except (TypeError, ValueError):
                continue
            if latest is None or value > latest:
                latest = value
    min_ts = None if full_audit or latest is None else max(0, int(latest.timestamp()) - _OVERLAP_SECONDS)
    return full_audit, min_ts, records, state


def _merge_history_rows(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]], key: str,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in (*existing, *incoming):
        if not isinstance(row, dict) or row.get(key) in (None, ""):
            continue
        merged[str(row[key])] = row
    return sorted(merged.values(), key=lambda row: str(
        row.get("created_time") or row.get("created_at") or row.get("settled_time") or ""))


def _execution_context_by_order(path: str) -> dict[str, dict[str, Any]]:
    """Join only exact venue order IDs; old fills without a stored join stay unattributed."""
    database = Path(path)
    if not database.is_file():
        return {}
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT provider_reference,request_json FROM execution_gateway_requests "
                "WHERE route='prediction' AND provider_reference IS NOT NULL "
                "ORDER BY created_at DESC"
            ).fetchall()
        result = {}
        for reference, raw in rows:
            if not reference or str(reference) in result:
                continue
            request = json.loads(raw) if isinstance(raw, str) else {}
            if not isinstance(request, dict):
                continue
            result[str(reference)] = {
                key: request.get(key) for key in ("decision_id", "strategy_id", "experiment_id")
                if request.get(key) is not None
            }
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {}


def cached_prediction_venue_status() -> dict[str, Any]:
    """Return the last official observation without initiating another venue read."""
    return copy.deepcopy(_cache["payload"] or {})


def invalidate_prediction_venue_status() -> None:
    """Make the next projection request reconcile a just-persisted account event."""
    _cache["at"] = 0.0


def _stamp_stream_metric(metric: dict[str, Any], account: dict[str, Any],
                         venue: dict[str, Any], observed_at: str) -> None:
    metric.update(
        source=venue.get("venue"), observed_at=observed_at, freshness="LIVE",
        coverage=copy.deepcopy(account.get("history_coverage", {})),
        reconciliation_state=(
            "RECONCILED" if metric.get("value") is not None and metric.get("reason") is None
            else "UNAVAILABLE"
        ),
    )


def apply_polymarket_stream_projection(update: dict[str, Any]) -> None:
    """Apply authenticated after-state events to the in-memory projection immediately.

    REST remains the periodic full-history and reconnect reconciliation. The stream
    only updates fields whose authoritative after-state arrived in the event.
    """
    payload = _cache.get("payload")
    if not isinstance(payload, dict):
        return
    venue = next((row for row in payload.get("venues", [])
                  if row.get("venue") == "Polymarket US"), None)
    account = venue.get("account") if isinstance(venue, dict) else None
    if not isinstance(account, dict) or account.get("status") != "authenticated_read_only":
        return
    observed_at = datetime.now(UTC).isoformat()
    changed = False
    balance_rows = update.get("balances") if isinstance(update.get("balances"), list) else []
    usd_balance = next((row for row in reversed(balance_rows)
                        if isinstance(row, dict) and str(row.get("currency", "")).upper() == "USD"), None)
    amount = _usd_amount(usd_balance.get("currentBalance")) if usd_balance else None
    if amount is not None:
        account["cash_balance_usd"] = amount
        account["observed_at"] = observed_at
        account["balance_available"] = True
        for field, source in (("buying_power_usd", "buyingPower"),
                              ("available_to_withdraw_usd", "availableToWithdraw")):
            value = _usd_amount(usd_balance.get(source))
            if value is not None:
                account[field] = value
        for row in account.get("cash_balances", []):
            if isinstance(row, dict) and str(row.get("currency", "")).upper() == "USD":
                row["current_balance"] = amount
                for field, source in (("buying_power", "buyingPower"),
                                      ("available_to_withdraw", "availableToWithdraw")):
                    value = _usd_amount(usd_balance.get(source))
                    if value is not None:
                        row[field] = value
                break
        capital = account.setdefault("capital", {})
        metrics = capital.setdefault("metrics", {})
        metrics["balance"] = _capital_metric(
            _decimal(amount), "VENUE_REPORTED", source_ids=["private-ws:account-balance"],
        )
        _stamp_stream_metric(metrics["balance"], account, venue, observed_at)
        account["account_metrics"] = metrics
        changed = True

    incoming_fills = update.get("fills") if isinstance(update.get("fills"), list) else []
    if incoming_fills:
        fills = [dict(row) for row in account.get("recent_fills", []) if isinstance(row, dict)]
        known = {str(row.get("fill_id")) for row in fills if row.get("fill_id")}
        added_volume = Decimal(0)
        added_count = 0
        added_source_ids: list[str] = []
        for row in incoming_fills:
            if not isinstance(row, dict) or not row.get("fill_id"):
                continue
            identity = str(row["fill_id"])
            if identity in known:
                continue
            known.add(identity)
            fills.append(dict(row))
            added_count += 1
            quantity, price = _decimal(row.get("count_fp")), _decimal(row.get("price_usd"))
            if quantity is not None and price is not None:
                added_volume += abs(quantity * price)
                added_source_ids.append(identity)
        if added_source_ids:
            metric = account.get("capital", {}).get("metrics", {}).get("volume", {})
            prior = _decimal(metric.get("value")) if isinstance(metric, dict) else None
            complete = account.get("history_coverage", {}).get("activities", {}).get("complete") is True
            if prior is not None and complete:
                total = prior + added_volume
                new_metric = _capital_metric(
                    total, "DERIVED_FROM_LIVE_RECORDS",
                    source_ids=[*metric.get("source_ids", []), *added_source_ids],
                    components={**(metric.get("components") or {}),
                                "stream_increment_usd": format(added_volume, "f")},
                )
                account["capital"]["metrics"]["volume"] = new_metric
                account.setdefault("account_metrics", {})["volume"] = new_metric
                _stamp_stream_metric(new_metric, account, venue, observed_at)
                account["capital"]["observed_volume_usd"] = format(total, "f")
        account["recent_fills"] = fills[-30:]
        account["fills"] = int(account.get("fills") or 0) + added_count
        account["observed_at"] = observed_at
        changed = True

    incoming_positions = update.get("positions") if isinstance(update.get("positions"), list) else []
    if incoming_positions:
        positions = [dict(row) for row in account.get("recent_positions", []) if isinstance(row, dict)]
        by_key = {(str(row.get("ticker")), str(row.get("outcome") or "")): index
                  for index, row in enumerate(positions)}
        for row in incoming_positions:
            if not isinstance(row, dict) or not row.get("ticker"):
                continue
            key = (str(row["ticker"]), str(row.get("outcome") or ""))
            normalized = dict(row)
            if key in by_key:
                positions[by_key[key]] = normalized
            else:
                by_key[key] = len(positions)
                positions.append(normalized)
        account["recent_positions"] = positions
        account["positions"] = sum(
            1 for row in positions if (_decimal(row.get("position_fp")) or Decimal(0)) != 0
        )
        coverage = account.get("history_coverage", {}).get("positions", {})
        if coverage.get("complete") is True:
            capital = account.setdefault("capital", {})
            refreshed = _polymarket_position_capital(
                positions, {"positions": positions, "eof": True}, [],
                activity_complete=False,
            )
            for key in ("exposure", "unrealized_pnl"):
                capital.setdefault("metrics", {})[key] = refreshed["metrics"][key]
                account.setdefault("account_metrics", {})[key] = refreshed["metrics"][key]
                _stamp_stream_metric(refreshed["metrics"][key], account, venue, observed_at)
        account["observed_at"] = observed_at
        changed = True

    incoming_orders = update.get("orders") if isinstance(update.get("orders"), list) else []
    if incoming_orders:
        orders = [dict(row) for row in account.get("recent_orders", []) if isinstance(row, dict)]
        by_id = {str(row.get("order_id")): index for index, row in enumerate(orders) if row.get("order_id")}
        for row in incoming_orders:
            if not isinstance(row, dict) or not row.get("order_id"):
                continue
            normalized = dict(row)
            key = str(row["order_id"])
            if key in by_id:
                orders[by_id[key]] = normalized
            else:
                by_id[key] = len(orders)
                orders.append(normalized)
        account["recent_orders"] = orders[-30:]
        account["open_orders"] = sum(
            str(row.get("status", "")).lower() in {"open", "resting", "pending"}
            for row in orders
        )
        account["observed_at"] = observed_at
        changed = True

    if changed:
        account["update_transport"] = polymarket_stream_health()
        _cache["at"] = monotonic()
        payload["as_of"] = observed_at


def _kalshi_account_metrics(
    balance: dict[str, Any], positions: dict[str, Any],
    fills: dict[str, Any], settlements: dict[str, Any],
    deposits: dict[str, Any] | None = None,
    withdrawals: dict[str, Any] | None = None,
    transfers: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fill_rows = [row for row in fills.get("fills", []) if isinstance(row, dict)]
    settlement_rows = [row for row in settlements.get("settlements", []) if isinstance(row, dict)]
    position_rows = [row for row in positions.get("market_positions", []) if isinstance(row, dict)]
    fills_complete = fills.get("pagination", {}).get("complete") is True
    settlements_complete = settlements.get("pagination", {}).get("complete") is True
    positions_complete = positions.get("pagination", {}).get("complete") is True
    open_position_count = _kalshi_open_position_count(positions.get("market_positions"))
    positions_flat = positions_complete and open_position_count == 0
    cashflows: list[Decimal | None] = []
    volume_values: list[Decimal | None] = []
    for row in fill_rows:
        outcome = str(row.get("outcome_side") or row.get("side") or "").lower()
        price_key = "yes_price_dollars" if outcome == "yes" else "no_price_dollars" if outcome == "no" else ""
        price, count = _decimal(row.get(price_key)), _decimal(row.get("count_fp"))
        book_side, action = str(row.get("book_side") or "").lower(), str(row.get("action") or "").lower()
        direction = Decimal(-1) if book_side == "bid" or (not book_side and action == "buy") else Decimal(1) if book_side == "ask" or action == "sell" else None
        if price is None or count is None or direction is None or count < 0:
            cashflows.append(None); volume_values.append(None)
        else:
            cashflows.append(direction * price * count)
            volume_values.append(price * count)
    volume = sum((v for v in volume_values if v is not None), Decimal(0)) if fills_complete and all(v is not None for v in volume_values) else None
    position_values = [_decimal(row.get("market_exposure_dollars")) for row in position_rows]
    exposure = (sum((abs(v) for v in position_values if v is not None), Decimal(0))
                if positions_complete and all(v is not None for v in position_values) else
                Decimal(0) if positions_complete and not position_rows else None)
    fill_fees = [_decimal(row.get("fee_cost")) for row in fill_rows]
    settlement_fees = [_decimal(row.get("fee_cost")) for row in settlement_rows]
    fee_values = [*fill_fees, *settlement_fees]
    fees = (sum((v for v in fee_values if v is not None), Decimal(0))
            if fills_complete and settlements_complete and all(v is not None for v in fee_values) else None)
    baseline_at: datetime | None = None
    baseline_equity: Decimal | None = None
    baseline_id = None
    if isinstance(baseline, dict):
        try:
            baseline_at = datetime.fromisoformat(str(baseline.get("observed_at")))
            baseline_equity = _decimal(baseline.get("cash_usd")) + _decimal(baseline.get("portfolio_value_usd"))
            baseline_id = f"account_baseline:{baseline.get('observed_at')}"
        except (TypeError, ValueError):
            baseline_at = baseline_equity = None

    def in_window(row: dict[str, Any], *fields: str) -> bool:
        if baseline_at is None:
            return False
        for field in fields:
            value = row.get(field)
            if value is None:
                continue
            try:
                try:
                    when = datetime.fromtimestamp(float(value), UTC)
                except (TypeError, ValueError, OverflowError, OSError):
                    when = datetime.fromisoformat(str(value))
                when = when.astimezone(UTC)
                return when > baseline_at.astimezone(UTC)
            except (TypeError, ValueError):
                continue
        return False

    window_fills = [row for row in fill_rows if in_window(row, "created_time", "created_ts")]
    window_settlements = [row for row in settlement_rows if in_window(row, "settled_time", "created_ts")]
    window_cashflows: list[Decimal | None] = []
    for row in window_fills:
        outcome = str(row.get("outcome_side") or row.get("side") or "").lower()
        price_key = "yes_price_dollars" if outcome == "yes" else "no_price_dollars" if outcome == "no" else ""
        price, count = _decimal(row.get(price_key)), _decimal(row.get("count_fp"))
        book_side, action = str(row.get("book_side") or "").lower(), str(row.get("action") or "").lower()
        direction = Decimal(-1) if book_side == "bid" or (not book_side and action == "buy") else Decimal(1) if book_side == "ask" or action == "sell" else None
        window_cashflows.append(None if price is None or count is None or direction is None or count < 0
                                else direction * price * count)
    window_revenues = [_decimal(row.get("revenue")) for row in window_settlements]
    window_fee_values = [_decimal(row.get("fee_cost")) for row in window_fills + window_settlements]
    window_fees = (sum((value for value in window_fee_values if value is not None), Decimal(0))
                   if all(value is not None for value in window_fee_values) else None)
    window_realized = (sum((value for value in window_cashflows if value is not None), Decimal(0))
                       + sum((value / 100 for value in window_revenues if value is not None), Decimal(0))
                       - window_fees
                       if all(value is not None for value in window_cashflows)
                       and all(value is not None for value in window_revenues)
                       and window_fees is not None else None)
    cash = _decimal(kalshi_cash_usd(balance))
    portfolio = _decimal(balance.get("portfolio_value_dollars"))
    if portfolio is None:
        raw_portfolio = _decimal(balance.get("portfolio_value"))
        portfolio = None if raw_portfolio is None else raw_portfolio / 100
    deposit_rows = (deposits or {}).get("deposits", [])
    withdrawal_rows = (withdrawals or {}).get("withdrawals", [])
    deposit_complete = (deposits or {}).get("pagination", {}).get("complete") is True
    withdrawal_complete = (withdrawals or {}).get("pagination", {}).get("complete") is True

    def external_flow(rows: list[dict[str, Any]], *, deposit: bool) -> Decimal | None:
        values = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            state = str(row.get("status", "")).lower()
            if state and state not in {"applied", "completed", "complete", "succeeded", "success"}:
                continue
            if not in_window(row, "created_time", "created_at", "created_ts", "finalized_ts"):
                # A completed flow without a timestamp cannot be assigned to
                # the opening-balance reconciliation window safely.
                if not any(row.get(key) is not None for key in ("created_time", "created_at", "created_ts", "finalized_ts")):
                    return None
                continue
            amount = _decimal(row.get("amount_dollars"))
            if amount is None:
                cents = _decimal(row.get("amount_cents"))
                amount = None if cents is None else cents / 100
            fee = _decimal(row.get("fee_dollars"))
            if fee is None:
                fee_cents = _decimal(row.get("fee_cents"))
                fee = Decimal(0) if fee_cents is None else fee_cents / 100
            if amount is None or fee is None:
                return None
            signed = amount - fee if deposit else -(amount + fee)
            values.append(signed)
        return sum(values, Decimal(0))

    deposits_net = external_flow(deposit_rows, deposit=True)
    withdrawals_net = external_flow(withdrawal_rows, deposit=False)
    transfers_rows = (transfers or {}).get("transfers", [])
    transfers_complete = (transfers or {}).get("pagination", {}).get("complete") is True
    transfer_scope_conflict = any(
        isinstance(row, dict) and row.get("transfer_type") == "subaccount"
        and in_window(row, "created_time", "created_ts")
        for row in transfers_rows
    )
    realized = window_realized
    cashflow_reconciliation = None
    if (cash is not None and portfolio is not None and baseline_equity is not None
            and deposit_complete and withdrawal_complete and transfers_complete
            and deposits_net is not None and withdrawals_net is not None and window_realized is not None
            and fills_complete and settlements_complete and positions_flat
            and not transfer_scope_conflict):
        end_equity = cash + portfolio
        external_net = deposits_net + withdrawals_net
        unexplained = end_equity - baseline_equity - external_net - window_realized
        cashflow_reconciliation = {
            "state": "UNRECONCILED",
            "window_start": baseline_at.astimezone(UTC).isoformat(),
            "ending_equity_usd": format(end_equity, "f"),
            "opening_equity_usd": format(baseline_equity, "f"),
            "net_external_flows_usd": format(external_net, "f"),
            "observed_trading_cashflow_after_costs_usd": format(window_realized, "f"),
            "unexplained_balance_delta_usd": format(unexplained, "f"),
            "deposit_source_ids": [str(row.get("id")) for row in deposit_rows if row.get("id")],
            "withdrawal_source_ids": [str(row.get("id")) for row in withdrawal_rows if row.get("id")],
            "reason": "The authenticated cash/position state does not reconcile to the flat opening snapshot, observed external flows, fills, settlements, and fees.",
        }
        if abs(unexplained) <= Decimal("0.01"):
            cashflow_reconciliation.update(state="RECONCILED", reason=None)
    realized = window_realized
    realized_reconciled = cashflow_reconciliation is not None and cashflow_reconciliation["state"] == "RECONCILED"
    realized_reason = (None if realized_reconciled else
        "A subaccount transfer occurred in the reconciliation window; source and destination scope cannot be netted safely." if transfer_scope_conflict else
        "Authenticated opening-balance history is unavailable; realized P&L cannot be reconciled." if baseline_equity is None else
        "Complete deposit, withdrawal, transfer, position, fill, and settlement coverage is required to reconcile realized P&L." if cashflow_reconciliation is None else
        cashflow_reconciliation.get("reason"))
    fill_ids = [str(row.get("fill_id")) for row in fill_rows if row.get("fill_id")]
    settlement_ids = [f"{row.get('ticker')}:{row.get('settled_time')}" for row in settlement_rows]
    realized_source_ids = [str(row.get("fill_id")) for row in window_fills if row.get("fill_id")]
    realized_source_ids.extend(f"{row.get('ticker')}:{row.get('settled_time')}" for row in window_settlements)
    if baseline_id:
        realized_source_ids.append(baseline_id)
    return {
        "balance": _capital_metric(cash, "VENUE_REPORTED" if cash is not None else "UNAVAILABLE",
            None if cash is not None else "Authenticated balance response omitted a valid USD balance.",
            source_ids=["/portfolio/balance:balance_dollars"] if cash is not None else []),
        "account_value": _capital_metric(portfolio, "VENUE_REPORTED" if portfolio is not None else "UNAVAILABLE",
            None if portfolio is not None else "Authenticated balance response omitted a valid portfolio_value.",
            source_ids=["/portfolio/balance:portfolio_value"] if portfolio is not None else []),
        "realized_pnl": _capital_metric(realized if realized_reconciled else None,
            "DERIVED_FROM_LIVE_RECORDS" if realized_reconciled else "UNAVAILABLE",
            realized_reason,
            source_ids=realized_source_ids,
            components={"observed_trading_cashflow_after_costs_usd": None if realized is None else format(realized, "f"),
                        "window_start": baseline_at.astimezone(UTC).isoformat() if baseline_at else None,
                        "window_end": _kalshi_balance_updated_at(balance.get("updated_ts")),
                        "pnl_scope": "reconciled since first complete flat authenticated balance; not lifetime P&L",
                        "fill_cashflows_usd": None if not all(v is not None for v in window_cashflows) else format(sum((v for v in window_cashflows if v is not None), Decimal(0)), "f"),
                        "settlement_revenue_cents": None if not all(v is not None for v in window_revenues) else format(sum((v for v in window_revenues if v is not None), Decimal(0)), "f"),
                        "fees_usd": None if window_fees is None else format(window_fees, "f"),
                        "cashflow_reconciliation": cashflow_reconciliation}),
        "unrealized_pnl": _capital_metric(None, "UNAVAILABLE",
            "Kalshi's live position response reports exposure but not both current liquidation value and remaining cost basis.",
            source_ids=[str(row.get("ticker")) for row in position_rows if row.get("ticker")]),
        "exposure": _capital_metric(exposure, "VENUE_REPORTED" if exposure is not None else "UNAVAILABLE",
            None if exposure is not None else ("Position pagination is incomplete." if not positions_complete else "An open position has no reported USD exposure."),
            source_ids=[str(row.get("ticker")) for row in position_rows if row.get("ticker")]),
        "volume": _capital_metric(volume, "DERIVED_FROM_LIVE_RECORDS" if volume is not None else "UNAVAILABLE",
            None if volume is not None else ("Live or historical fill pagination is incomplete." if not fills_complete else "A fill is missing a supported outcome price or contract quantity."),
            source_ids=fill_ids, components={"basis": "sum(contract quantity × outcome price)"}),
        "fees": _capital_metric(fees, "DERIVED_FROM_LIVE_RECORDS" if fees is not None else "UNAVAILABLE",
            None if fees is not None else ("Fill or settlement pagination is incomplete." if not fills_complete or not settlements_complete else "At least one fill or settlement is missing fee_cost."),
            source_ids=fill_ids + settlement_ids, components={"basis": "fill fee_cost + settlement fee_cost"}),
    }


def _kalshi_open_position_count(rows: Any) -> int | None:
    """Count only nonzero authenticated positions; unknown quantity is not flat."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    quantities = [_decimal(row.get("position_fp")) for row in rows]
    if any(quantity is None for quantity in quantities):
        return None
    return sum(quantity != 0 for quantity in quantities)


def _polymarket_activity_keys(row: Any) -> set[str]:
    if not isinstance(row, dict):
        return set()
    kind = str(row.get("type") or "")
    nested = next((row.get(key) for key in ("trade", "positionResolution", "accountBalanceChange")
                   if isinstance(row.get(key), dict)), {})
    identities = [nested.get("id"), nested.get("tradeId")]
    transactions = nested.get("transactions") if isinstance(nested, dict) else None
    if isinstance(transactions, list):
        identities.extend(item.get("transactionId") for item in transactions if isinstance(item, dict))
    return {f"{kind}:{identity}" for identity in identities if identity not in (None, "")}


async def _polymarket_cursor_pages(
    method: Any, collection: str, *, stop_after_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch new activity through a persisted boundary, or audit all cursor pages."""
    cursor: str | None = None
    seen: set[str] = set()
    seen_records: set[str] = set()
    pages, complete, reason = 0, True, None
    stop_after_ids = stop_after_ids or set()
    reached_anchor = False
    records: dict[str, Any] | list[Any] = {} if collection == "positions" else []
    while pages < 10000:
        params: dict[str, Any] = {"limit": 100}
        if collection == "activities":
            params["sortOrder"] = "SORT_ORDER_DESCENDING"
        if cursor:
            if cursor in seen:
                complete, reason = False, "repeated_cursor"
                break
            seen.add(cursor); params["cursor"] = cursor
        payload = await asyncio.to_thread(method, params)
        if not isinstance(payload, dict):
            complete, reason = False, "malformed_page"
            break
        page = payload.get(collection)
        if collection == "positions" and isinstance(page, dict):
            records.update(page)
        elif isinstance(page, list):
            for item in page:
                if collection == "activities" and isinstance(item, dict):
                    keys = _polymarket_activity_keys(item)
                    if stop_after_ids.intersection(keys):
                        reached_anchor = True
                    duplicate = bool(seen_records.intersection(keys)) if keys else False
                    seen_records.update(keys)
                    if duplicate:
                        continue
                records.append(item)
        else:
            complete, reason = False, "missing_record_collection"
            break
        pages += 1
        if reached_anchor:
            break
        next_cursor = payload.get("nextCursor")
        if payload.get("eof") is True or not next_cursor:
            if payload.get("eof") is False and not next_cursor:
                complete, reason = False, "provider_marked_not_eof_without_cursor"
            break
        if not isinstance(next_cursor, str):
            complete, reason = False, "malformed_cursor"
            break
        cursor = next_cursor
    else:
        complete, reason = False, "page_limit_reached"
    delta_complete = complete and (reached_anchor or not stop_after_ids)
    return {collection: records, "pagination": {
        "complete": complete, "delta_complete": delta_complete,
        "incremental": bool(stop_after_ids), "reached_anchor": reached_anchor, "pages": pages,
        "records": len(records), "reason": reason,
    }, "eof": complete}


async def _kalshi_status() -> dict[str, Any]:
    config = KalshiConfig.from_env()
    # Real production read-only telemetry is separate from the configured order
    # environment. No setting or execution authority is changed by this check.
    production = kalshi_production_read_only_config()
    started = monotonic()
    venue = KalshiVenue(production)
    try:
        markets, cursor = await venue.market_page(limit=10)
        exchange = await venue.exchange_status()
        open_market = markets[0] if markets else None
        book_ok = False
        book_levels = 0
        if open_market:
            response = await venue.client.get(f"/markets/{open_market.market_id}/orderbook")
            if response.status_code == 200:
                payload = response.json()
                book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
                book_levels = sum(
                    len(book.get(side) or []) for side in ("yes_dollars", "no_dollars", "yes", "no")
                )
                book_ok = True
        market_data = {
            "status": "connected",
            "open_markets_sampled": len(markets),
            "more_markets_available": bool(cursor),
            "order_book_readable": book_ok,
            "order_book_levels": book_levels if book_ok else None,
            "sample_market": None if open_market is None else {
                "market_id": open_market.market_id,
                "title": open_market.title,
                "yes_bid": open_market.yes_bid,
                "yes_ask": open_market.yes_ask,
                "spread": (
                    None if open_market.yes_bid is None or open_market.yes_ask is None
                    else round(open_market.yes_ask - open_market.yes_bid, 6)
                ),
                "closes_at": None if open_market.closes_at is None else open_market.closes_at.isoformat(),
                "rules_available": bool(open_market.resolution_rules),
                "liquidity_usd": open_market.liquidity_usd,
            },
            "exchange_trading_active": exchange.get("trading_active"),
        }
    except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
            KeyError, TypeError, OSError):
        market_data = {"status": "degraded"}
    finally:
        await venue.close()

    keychain_present = config.key_id is not None
    pem = Path(config.private_key_path) if config.private_key_path else None
    pem_file_status = (
        "environment"
        if config.private_key_pem is not None or config.private_key_pem_b64 is not None
        else private_key_file_status(pem)
    )
    render_secret_file = config.private_key_source == "render_secret_file"
    try:
        pem_mode_is_private = bool(pem and (pem.stat().st_mode & 0o077) == 0)
    except OSError:
        pem_mode_is_private = False
    protected_key_present = bool(
        config.private_key_pem is not None
        or config.private_key_pem_b64 is not None
        or (pem_file_status == "readable" and (render_secret_file or pem_mode_is_private))
    )
    account: dict[str, Any] = {
        "status": "unconfigured",
        "credential_presence": {
            "api_key_id": keychain_present,
            "private_key": (
                config.private_key_pem is not None or config.private_key_pem_b64 is not None
                or pem is not None
            ),
            "api_key_id_provider": credential_source(
                "KALSHI_API_KEY_ID", keychain_service=KALSHI_KEY_ID_KEYCHAIN_SERVICE,
            ),
            "private_key_provider": config.private_key_source,
            "private_key_file_status": pem_file_status,
        },
        "balance_available": False,
        "positions": None,
        "open_orders": None,
        "fills": None,
    }
    if keychain_present and protected_key_present:
        account_config = KalshiConfig(
            environment="production", key_id=config.key_id,
            private_key_path=config.private_key_path,
            private_key_pem=config.private_key_pem,
            private_key_pem_b64=config.private_key_pem_b64,
            private_key_source=config.private_key_source,
            allow_live_orders=False, master_halt=True,
        )
        client: KalshiAccount | None = None
        try:
            client = KalshiAccount(account_config)
            full_audit, min_ts, history, sync_state = _account_history_plan(
                "kalshi", ("fill", "settlement", "deposit", "withdrawal", "transfer"),
                required_streams=("activity", "settlement", "deposit", "withdrawal", "transfer"),
            )
            snapshot = await client.snapshot(
                min_ts=min_ts, full_history=full_audit,
            )
            def settlement_key(row: dict[str, Any]) -> str:
                return str(row.get("settlement_id") or
                           f"{row.get('ticker')}:{row.get('settled_time')}:{row.get('exchange_index', 0)}")
            prior_activity_complete = bool((sync_state.get("activity") or {}).get("last_full_audit_at"))
            prior_settlement_complete = bool((sync_state.get("settlement") or {}).get("last_full_audit_at"))
            prior_deposit_complete = bool((sync_state.get("deposit") or {}).get("last_full_audit_at"))
            prior_withdrawal_complete = bool((sync_state.get("withdrawal") or {}).get("last_full_audit_at"))
            fill_ids_observed = {str(row.get("fill_id")) for row in snapshot.fills.get("fills", []) if row.get("fill_id")}
            old_fill_ids = {str(row.get("fill_id")) for row in history.get("fill", []) if row.get("fill_id")}
            settlement_ids_observed = {settlement_key(row) for row in snapshot.settlements.get("settlements", [])}
            old_settlement_ids = {settlement_key(row) for row in history.get("settlement", [])}
            missing_fill_ids = old_fill_ids - fill_ids_observed if full_audit else set()
            missing_settlement_ids = old_settlement_ids - settlement_ids_observed if full_audit else set()
            fills_complete = (snapshot.fills.get("pagination", {}).get("complete") is True
                              and (full_audit or prior_activity_complete) and not missing_fill_ids)
            settlements_complete = (snapshot.settlements.get("pagination", {}).get("complete") is True
                                    and (full_audit or prior_settlement_complete) and not missing_settlement_ids)
            deposit_ids_observed = {str(row.get("id")) for row in snapshot.deposits.get("deposits", []) if row.get("id")}
            withdrawal_ids_observed = {str(row.get("id")) for row in snapshot.withdrawals.get("withdrawals", []) if row.get("id")}
            old_deposit_ids = {str(row.get("id")) for row in history.get("deposit", []) if row.get("id")}
            old_withdrawal_ids = {str(row.get("id")) for row in history.get("withdrawal", []) if row.get("id")}
            missing_deposit_ids = old_deposit_ids - deposit_ids_observed if full_audit else set()
            missing_withdrawal_ids = old_withdrawal_ids - withdrawal_ids_observed if full_audit else set()
            deposits_complete = (snapshot.deposits.get("pagination", {}).get("complete") is True
                                 and (full_audit or prior_deposit_complete) and not missing_deposit_ids)
            withdrawals_complete = (snapshot.withdrawals.get("pagination", {}).get("complete") is True
                                    and (full_audit or prior_withdrawal_complete) and not missing_withdrawal_ids)
            all_fills = _merge_history_rows(history.get("fill", []), snapshot.fills.get("fills", []), "fill_id")
            all_settlements_by_id = {settlement_key(row): row for row in history.get("settlement", [])}
            all_settlements_by_id.update({settlement_key(row): row for row in snapshot.settlements.get("settlements", [])})
            all_settlements = list(all_settlements_by_id.values())
            execution_context = _execution_context_by_order(os.getenv("NOEMA_DB_PATH", "data/noema.db"))
            for row in all_fills:
                context = execution_context.get(str(row.get("order_id"))) if row.get("order_id") else None
                if context:
                    row.update(context)
            metric_fills = {**snapshot.fills, "fills": all_fills,
                            "pagination": {**snapshot.fills.get("pagination", {}), "complete": fills_complete}}
            metric_settlements = {**snapshot.settlements, "settlements": all_settlements,
                                  "pagination": {**snapshot.settlements.get("pagination", {}), "complete": settlements_complete}}
            all_deposits = _merge_history_rows(history.get("deposit", []), snapshot.deposits.get("deposits", []), "id")
            all_withdrawals = _merge_history_rows(history.get("withdrawal", []), snapshot.withdrawals.get("withdrawals", []), "id")
            metric_deposits = {**snapshot.deposits, "deposits": all_deposits,
                               "pagination": {**snapshot.deposits.get("pagination", {}), "complete": deposits_complete}}
            metric_withdrawals = {**snapshot.withdrawals, "withdrawals": all_withdrawals,
                                  "pagination": {**snapshot.withdrawals.get("pagination", {}), "complete": withdrawals_complete}}
            positions_complete = snapshot.positions.get("pagination", {}).get("complete") is True
            capital = kalshi_observed_capital(snapshot.positions)
            all_transfers = _merge_history_rows(
                history.get("transfer", []), snapshot.transfers.get("transfers", []),
                "transfer_id",
            )
            transfer_ids_observed = {
                str(row.get("transfer_id") or row.get("id"))
                for row in snapshot.transfers.get("transfers", [])
                if row.get("transfer_id") or row.get("id")
            }
            old_transfer_ids = {
                str(row.get("transfer_id") or row.get("id"))
                for row in history.get("transfer", [])
                if row.get("transfer_id") or row.get("id")
            }
            missing_transfer_ids = old_transfer_ids - transfer_ids_observed if full_audit else set()
            transfers_complete = (
                snapshot.transfers.get("pagination", {}).get("complete") is True
                and not missing_transfer_ids
            )
            metric_transfers = {
                **snapshot.transfers, "transfers": all_transfers,
                "pagination": {**snapshot.transfers.get("pagination", {}),
                               "complete": transfers_complete},
            }
            open_position_count = (
                _kalshi_open_position_count(snapshot.positions.get("market_positions"))
                if positions_complete else None
            )
            balance_updated_at = _kalshi_balance_updated_at(snapshot.balance.get("updated_ts"))
            baseline = get_or_create_prediction_account_baseline(
                console_state_db_path(), "kalshi",
                observed_at=balance_updated_at or "",
                cash_usd=kalshi_cash_usd(snapshot.balance),
                portfolio_value_usd=(
                    _usd_amount(snapshot.balance.get("portfolio_value_dollars"))
                    if snapshot.balance.get("portfolio_value_dollars") is not None else
                    (None if _decimal(snapshot.balance.get("portfolio_value")) is None else
                     format((_decimal(snapshot.balance.get("portfolio_value")) / 100).normalize(), "f"))
                ),
                positions_complete=positions_complete,
                open_positions=open_position_count,
            )
            account_metrics = _kalshi_account_metrics(
                snapshot.balance, snapshot.positions, metric_fills, metric_settlements,
                metric_deposits, metric_withdrawals, metric_transfers, baseline,
            )
            # The current positions endpoint omits closed markets. Project the
            # existing account fields from full historical fills/settlements
            # when their pagination and per-record costs reconcile.
            for target, source in (
                ("observed_volume_usd", "volume"),
                ("reported_realized_pnl_usd", "realized_pnl"),
                ("reported_fees_usd", "fees"),
                ("reported_exposure_usd", "exposure"),
            ):
                capital[target] = account_metrics[source]["value"]
            capital["metrics"] = account_metrics
            account = {
                "status": "authenticated_read_only",
                "balance_available": bool(snapshot.balance),
                "cash_balance_usd": kalshi_cash_usd(snapshot.balance),
                "portfolio_value_usd": (
                    _usd_amount(snapshot.balance.get("portfolio_value_dollars"))
                    if snapshot.balance.get("portfolio_value_dollars") is not None else
                    (None if _decimal(snapshot.balance.get("portfolio_value")) is None else
                     format((_decimal(snapshot.balance.get("portfolio_value")) / 100).normalize(), "f"))
                ),
                "observed_at": datetime.now(UTC).isoformat(),
                "capital": capital,
                "account_metrics": account_metrics,
                "positions": open_position_count,
                "open_orders": sum(
                    str(order.get("status", "")).lower() in {"resting", "open", "pending"}
                    for order in snapshot.orders.get("orders", [])
                ),
                "fills": len(all_fills),
                "recent_fills": [{key: row.get(key) for key in (
                    "fill_id", "order_id", "ticker", "outcome_side", "book_side", "side", "action", "count_fp",
                    "yes_price_dollars", "no_price_dollars", "fee_cost", "created_time",
                )} for row in all_fills[-30:] if isinstance(row, dict)],
                "settlements": [{key: row.get(key) for key in (
                    "ticker", "event_ticker", "market_result", "yes_count_fp", "no_count_fp",
                    "yes_total_cost_dollars", "no_total_cost_dollars", "revenue", "fee_cost",
                    "settled_time", "exchange_index",
                )} for row in all_settlements[-30:] if isinstance(row, dict)],
                "history_coverage": {
                    "positions": snapshot.positions.get("pagination", {}),
                    "orders": snapshot.orders.get("pagination", {}),
                    "fills": {**snapshot.fills.get("pagination", {}), "complete": fills_complete,
                               "full_audit": full_audit, "known_history_complete": prior_activity_complete or full_audit,
                               "missing_persisted_records": len(missing_fill_ids),
                               "reason": "authoritative_full_scan_missing_persisted_fill_ids" if missing_fill_ids else snapshot.fills.get("pagination", {}).get("reason")},
                    "settlements": {**snapshot.settlements.get("pagination", {}), "complete": settlements_complete,
                                     "full_audit": full_audit, "known_history_complete": prior_settlement_complete or full_audit,
                                     "missing_persisted_records": len(missing_settlement_ids),
                                     "reason": "authoritative_full_scan_missing_persisted_settlement_ids" if missing_settlement_ids else snapshot.settlements.get("pagination", {}).get("reason")},
                        "deposits": {**snapshot.deposits.get("pagination", {}), "complete": deposits_complete,
                                     "full_audit": full_audit, "missing_persisted_records": len(missing_deposit_ids)},
                    "withdrawals": {**snapshot.withdrawals.get("pagination", {}), "complete": withdrawals_complete,
                                    "full_audit": full_audit, "missing_persisted_records": len(missing_withdrawal_ids)},
                    "transfers": {**snapshot.transfers.get("pagination", {}),
                                  "complete": transfers_complete,
                                  "full_audit": full_audit,
                                  "missing_persisted_records": len(missing_transfer_ids)},
                },
                "_persisted_records": {
                    # Persist only records returned by this delta/full-audit
                    # request; previously known immutable fills stay untouched.
                    "fills": snapshot.fills.get("fills", []),
                    "orders": snapshot.orders.get("orders", []),
                    "settlements": snapshot.settlements.get("settlements", []),
                    "deposits": snapshot.deposits.get("deposits", []),
                    "withdrawals": snapshot.withdrawals.get("withdrawals", []),
                    "transfers": snapshot.transfers.get("transfers", []),
                    "positions": snapshot.positions.get("market_positions", []),
                    "balance": {"observed_at": datetime.now(UTC).isoformat(),
                                "cash_balance_usd": kalshi_cash_usd(snapshot.balance),
                                "portfolio_value_usd": account.get("portfolio_value_usd")},
                    "sync_state": {
                        "activity": {"success": snapshot.fills.get("pagination", {}).get("complete") is True and not missing_fill_ids,
                            "complete": snapshot.fills.get("pagination", {}).get("complete") is True and not missing_fill_ids,
                            "full_audit_complete": full_audit and snapshot.fills.get("pagination", {}).get("complete") is True and not missing_fill_ids,
                            "high_water_at": max((str(row.get("created_time")) for row in snapshot.fills.get("fills", []) if row.get("created_time")), default=None),
                            "high_water_id": snapshot.fills.get("fills", [])[-1].get("fill_id") if snapshot.fills.get("fills") else None},
                        "settlement": {"success": snapshot.settlements.get("pagination", {}).get("complete") is True and not missing_settlement_ids,
                            "complete": snapshot.settlements.get("pagination", {}).get("complete") is True and not missing_settlement_ids,
                            "full_audit_complete": full_audit and snapshot.settlements.get("pagination", {}).get("complete") is True and not missing_settlement_ids,
                            "high_water_at": max((str(row.get("settled_time")) for row in snapshot.settlements.get("settlements", []) if row.get("settled_time")), default=None),
                            "high_water_id": settlement_key(snapshot.settlements.get("settlements", [])[-1]) if snapshot.settlements.get("settlements") else None},
                        "deposit": {"success": deposits_complete,
                            "complete": deposits_complete,
                            "full_audit_complete": full_audit and deposits_complete},
                        "withdrawal": {"success": withdrawals_complete,
                            "complete": withdrawals_complete,
                            "full_audit_complete": full_audit and withdrawals_complete},
                        "transfer": {"success": transfers_complete,
                            "complete": transfers_complete,
                            "full_audit_complete": full_audit and transfers_complete},
                    },
                },
                "recent_orders": [{key: row.get(key) for key in (
                    "order_id", "ticker", "status", "fill_count_fp", "remaining_count_fp",
                    "created_time", "last_update_time",
                )} for row in snapshot.orders.get("orders", [])[:30] if isinstance(row, dict)],
                "limits_available": bool(snapshot.limits),
                "user_data_as_of": (
                    None if snapshot.user_data_as_of is None
                    else snapshot.user_data_as_of.isoformat()
                ),
            }
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
            account["status"] = "authentication_failed"
            account["account_read"] = {"complete": False, "reason": "An authenticated Kalshi account endpoint failed; account totals were not projected."}
        finally:
            if client is not None:
                await client.close()
    elif not keychain_present:
        account["status"] = "missing_key_id"
    elif not protected_key_present:
        account["status"] = "missing_or_unprotected_private_key"

    return {
        "venue": "Kalshi",
        "environment": "production_read_only",
        "execution": "disabled_in_this_read_only_check",
        "latency_ms": round((monotonic() - started) * 1000),
        "market_data": market_data,
        "account": account,
    }


def _polymarket_account_error(exc: BaseException) -> dict[str, Any]:
    """Return safe request-failure diagnostics without SDK message/body/URL data."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        response = getattr(exc, "response", None)
        candidate = getattr(response, "status_code", None)
        status = candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None
    name = type(exc).__name__
    if status in {401, 403}:
        classification = "authenticated_api_rejection"
    elif status == 429:
        classification = "rate_limited"
    elif status is not None and status >= 500:
        classification = "upstream_unavailable"
    elif status is not None:
        classification = "api_rejection"
    elif name in {"APIConnectionError", "APITimeoutError"} or isinstance(
        exc, (httpx.RequestError, TimeoutError),
    ):
        classification = "network_failure"
    elif isinstance(exc, (ValueError, KeyError, TypeError)):
        classification = "malformed_response"
    elif isinstance(exc, sqlite3.Error):
        classification = "local_persistence_failure"
    else:
        classification = "account_request_failure"
    return {"classification": classification, "error_type": name, "http_status": status}


async def _polymarket_us_status() -> dict[str, Any]:
    started = monotonic()
    venue = PolymarketUSVenue()
    try:
        markets: list[Any] = []
        sample = None
        book: dict[str, Any] | None = None
        market_data: dict[str, Any]
        try:
            markets, _ = await venue.market_page(limit=1)
            sample = next((item for item in markets if item.yes_bid is not None or item.yes_ask is not None), None)
            if sample:
                try:
                    book = await venue.book(sample.market_id)
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError):
                    pass
            market_data = {
                "status": "connected",
                "open_markets_sampled": len(markets),
                "sample_market": None if sample is None else {
                    "market_id": sample.market_id,
                    "title": sample.title,
                    "yes_bid": sample.yes_bid,
                    "yes_ask": sample.yes_ask,
                    "spread": (
                        None if sample.yes_bid is None or sample.yes_ask is None
                        else round(sample.yes_ask - sample.yes_bid, 6)
                    ),
                    "closes_at": None if sample.closes_at is None else sample.closes_at.isoformat(),
                    "rules_available": bool(sample.resolution_rules),
                    # USD depth remains unknown until actual book levels can be
                    # interpreted consistently with contract units.
                    "liquidity_usd": sample.liquidity_usd,
                },
                "order_book_readable": book is not None,
                "order_book_levels": (
                    None if book is None else len(book.get("bids", [])) + len(book.get("offers", []))
                ),
                "market_state": None if book is None else book.get("state"),
            }
        except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                KeyError, TypeError, OSError) as exc:
            market_error = _polymarket_account_error(exc)
            market_data = {
                "status": "degraded",
                "failure_classification": market_error["classification"],
                "error_type": market_error["error_type"],
                "http_status": market_error["http_status"],
            }
        key_id_present, secret_present = polymarket_us_credentials_present()
        account: dict[str, Any] = {
            "status": "unconfigured",
            "credential_presence": {
                "key_id": key_id_present,
                "secret_key": secret_present,
                "providers": polymarket_us_credential_sources(),
            },
            "private_stream": polymarket_stream_health(),
        }
        if key_id_present and secret_present:
            account_status = "authenticated_read_only"
            authenticated_client = None
            request_stage = "credential_load"
            try:
                from polymarket_us import PolymarketUS

                key_id, secret_key = load_polymarket_us_credentials_in_api_boundary()
                authenticated_client = PolymarketUS(
                    key_id=key_id, secret_key=secret_key, timeout=5.0, max_retries=0,
                )
                request_stage = "balances"
                balances = await asyncio.to_thread(authenticated_client.account.balances)
                request_stage = "account_history_plan"
                full_audit, _min_ts, history, sync_state = _account_history_plan(
                    "polymarket_us", ("fill", "position_resolution", "balance_activity"),
                    required_streams=("activity",),
                )
                request_stage = "positions"
                positions = await _polymarket_cursor_pages(
                    authenticated_client.portfolio.positions, "positions",
                )
                known_activity_ids: set[str] = set()
                known_activity_ids.update(f"ACTIVITY_TYPE_TRADE:{row.get('fill_id')}"
                                          for row in history.get("fill", []) if row.get("fill_id"))
                known_activity_ids.update(f"ACTIVITY_TYPE_POSITION_RESOLUTION:{row.get('trade_id')}"
                                          for row in history.get("position_resolution", []) if row.get("trade_id"))
                known_activity_ids.update(f"{row.get('activity_type')}:{row.get('transaction_id')}"
                                          for row in history.get("balance_activity", []) if row.get("transaction_id"))
                request_stage = "activities"
                activities = await _polymarket_cursor_pages(
                    authenticated_client.portfolio.activities, "activities",
                    stop_after_ids=set() if full_audit else known_activity_ids,
                )
                observed_activity_ids = set().union(*(
                    _polymarket_activity_keys(row) for row in activities.get("activities", [])
                )) if activities.get("activities") else set()
                missing_activity_ids = known_activity_ids - observed_activity_ids if full_audit else set()
                activity_history_complete = (
                    activities.get("pagination", {}).get("complete") is True
                    and (full_audit or bool((sync_state.get("activity") or {}).get("last_full_audit_at")))
                    and not missing_activity_ids
                )
                request_stage = "orders"
                orders = await asyncio.to_thread(authenticated_client.orders.list)
                position_rows = _polymarket_positions(positions)
                delta_fills = _polymarket_fills(activities)
                recent_fills = _merge_history_rows(history.get("fill", []), delta_fills, "fill_id")
                delta_resolutions = _polymarket_resolutions(activities)
                resolution_rows = _merge_history_rows(
                    history.get("position_resolution", []), delta_resolutions, "trade_id",
                )
                delta_balance_activities = _polymarket_balance_activities(activities)
                balance_activities = _merge_history_rows(
                    history.get("balance_activity", []), delta_balance_activities, "transaction_id",
                )
                recent_orders = _polymarket_orders(orders)
                cash_balances = []
                raw_balances = balances.get("balances", []) if isinstance(balances, dict) else []
                if isinstance(raw_balances, list):
                    for item in raw_balances:
                        if not isinstance(item, dict):
                            continue
                        cash_balances.append({
                            "currency": str(item.get("currency") or "unknown"),
                            "current_balance": _usd_amount(item.get("currentBalance")),
                            "buying_power": _usd_amount(item.get("buyingPower")),
                            "available_to_withdraw": _usd_amount(item.get("availableToWithdraw")),
                        })
                usd_balance = next(
                    (item for item in cash_balances if item["currency"].upper() == "USD"),
                    None,
                )
                polymarket_capital = _polymarket_position_capital(
                    position_rows, positions, recent_fills,
                    activity_complete=activity_history_complete,
                    resolution_rows=resolution_rows,
                    activity_incomplete_reason=(
                        f"A complete Polymarket activity audit no longer returned {len(missing_activity_ids)} persisted activity IDs; "
                        "the immutable local history was retained and account-wide activity totals are withheld."
                        if missing_activity_ids else None
                    ),
                )
                polymarket_capital["metrics"]["balance"] = _capital_metric(
                    None if usd_balance is None else _decimal(usd_balance["current_balance"]),
                    "VENUE_REPORTED" if usd_balance is not None and usd_balance["current_balance"] is not None else "UNAVAILABLE",
                    None if usd_balance is not None and usd_balance["current_balance"] is not None else
                    "Authenticated balances response contains no valid USD currentBalance.",
                    source_ids=["/v1/account/balances:USD.currentBalance"] if usd_balance is not None else [],
                )
                request_stage = "normalize_and_reconcile"
                account = {
                    "status": account_status,
                    "update_transport": polymarket_stream_health(),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "balance_available": bool(balances),
                    "cash_balances": cash_balances,
                    "cash_balance_usd": None if usd_balance is None else usd_balance["current_balance"],
                    "buying_power_usd": None if usd_balance is None else usd_balance["buying_power"],
                    "available_to_withdraw_usd": None if usd_balance is None else usd_balance["available_to_withdraw"],
                    "positions": _count_records(positions, "positions"),
                    "open_orders": _count_records(orders, "orders"),
                    "fills": len(recent_fills),
                    "activity_records": _count_records(activities, "activities"),
                    "capital": polymarket_capital,
                    "recent_positions": position_rows,
                    "recent_fills": recent_fills[-30:],
                    "recent_orders": recent_orders[-30:],
                    "resolutions": resolution_rows,
                    "balance_activities": balance_activities,
                    "history_coverage": {
                        "positions": positions.get("pagination", {}),
                        "activities": {**activities.get("pagination", {}),
                            "complete": activity_history_complete,
                            "api_scan_complete": activities.get("pagination", {}).get("complete") is True,
                            "known_history_complete": full_audit or bool((sync_state.get("activity") or {}).get("last_full_audit_at")),
                            "full_audit": full_audit,
                            "missing_persisted_records": len(missing_activity_ids),
                            "reason": "authoritative_full_scan_missing_persisted_activity_ids" if missing_activity_ids else activities.get("pagination", {}).get("reason")},
                        "orders": {"complete": isinstance(orders, dict), "open_snapshot": isinstance(orders, dict), "pages": 1},
                    },
                    "_persisted_records": {
                        "fills": delta_fills,
                        "orders": recent_orders,
                        "positions": position_rows,
                        "position_resolutions": delta_resolutions,
                        "balance_activities": delta_balance_activities,
                        "balance": {"observed_at": datetime.now(UTC).isoformat(),
                                    "cash_balance_usd": None if usd_balance is None else usd_balance["current_balance"]},
                        "sync_state": {"activity": {
                            "success": activities.get("pagination", {}).get("delta_complete") is True and not missing_activity_ids,
                            "complete": activities.get("pagination", {}).get("delta_complete") is True and not missing_activity_ids,
                            "full_audit_complete": full_audit and activities.get("pagination", {}).get("complete") is True and not missing_activity_ids,
                            "high_water_at": max((str(row.get("created_time")) for row in delta_fills if row.get("created_time")), default=None),
                            "high_water_id": delta_fills[-1].get("fill_id") if delta_fills else None,
                        }},
                    },
                }
            except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                    KeyError, TypeError, OSError, sqlite3.Error) as exc:
                account_status = "authentication_or_read_failed"
                error = _polymarket_account_error(exc)
                account = {
                    "status": account_status,
                    "account_read": {
                        "complete": False,
                        "stage": request_stage,
                        **error,
                        "reason": (
                            "Authenticated Polymarket US account request failed at "
                            f"{request_stage} ({error['classification']}); source totals were not projected."
                        ),
                    },
                    "balance_available": False,
                    "positions": None,
                    "open_orders": None,
                    "activity_records": None,
                }
            finally:
                if authenticated_client is not None:
                    authenticated_client.close()
        elif key_id_present:
            account_status = "missing_secret_key"
            account = {"status": account_status}
        elif secret_present:
            account_status = "missing_key_id"
            account = {"status": account_status}
        else:
            account_status = "missing_key_id_and_secret_key"
            account = {"status": account_status}
        account["credential_presence"] = {
            "key_id": key_id_present,
            "secret_key": secret_present,
            "providers": polymarket_us_credential_sources(),
        }
        account["private_stream"] = polymarket_stream_health()
        return {
            "venue": "Polymarket US",
            "environment": "production_read_only_public_data",
            "execution": "disabled",
            "latency_ms": round((monotonic() - started) * 1000),
            "market_data": market_data,
            "account": account,
        }
    except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
            KeyError, TypeError, OSError, sqlite3.Error) as exc:
        error = _polymarket_account_error(exc)
        return {
            "venue": "Polymarket US",
            "environment": "production_read_only_public_data",
            "execution": "disabled",
            "latency_ms": round((monotonic() - started) * 1000),
            "market_data": {"status": "degraded"},
            "account": {
                "status": "account_read_failed",
                "account_read": {
                    "complete": False,
                    "stage": "venue_status",
                    **error,
                    "reason": "Polymarket status assembly failed; inspect only the safe error classification.",
                },
            },
        }
    finally:
        venue.close()


async def _cross_venue_candidate() -> dict[str, Any]:
    """Discover one current public same-outcome candidate; never infer edge."""
    global _registered_quote_pair, _registered_identity_review_at
    now_mono = monotonic()
    if _registered_quote_pair is None:
        _registered_quote_pair = _load_registered_quote_pair()
        if _registered_quote_pair is not None:
            # A restart loads established identities from the canonical graph;
            # persisted links remain the fast-path source of truth.
            _registered_identity_review_at = now_mono
    if _registered_quote_pair is not None and now_mono - _registered_identity_review_at < 900:
        # Fast path: use persisted/previously established contract identities;
        # this refresh reads quotes only and deliberately does not run resolver
        # or settlement reasoning on each quote update.
        refreshed_pairs = await asyncio.gather(*(
            _refresh_registered_pair(pair) for pair in _registered_quote_pair
        ))
        if refreshed_pairs and all(pair is not None for pair in refreshed_pairs):
            return {
                "status": "candidate_only", "matches": refreshed_pairs,
                "reason": "Registered canonical proposition; quotes refreshed without rerunning identity resolution.",
                "path": "fast_quote_refresh",
            }
    kalshi = KalshiVenue(KalshiConfig(
        environment="production", allow_live_orders=False, master_halt=True,
    ))
    polymarket = PolymarketUSVenue()
    try:
        kalshi_response = await kalshi.client.get(
            "/events",
            params={"series_ticker": "KXMLB", "status": "open", "limit": 100,
                    "with_nested_markets": "true"},
        )
        kalshi_response.raise_for_status()
        kalshi_events = kalshi_response.json().get("events", [])
        kalshi_received_at = datetime.now(UTC).isoformat()
        if not kalshi_events:
            return {"status": "no_current_candidate", "matches": [], "reason": "No active Kalshi MLB champion event."}

        season = datetime.now(UTC).year
        search = await asyncio.to_thread(
            polymarket.client.search.query,
            {"query": f"{season} MLB World Series Champion"},
        )
        events = search.get("events", []) if isinstance(search, dict) else []
        chosen_event = next((
            event for event in events
            if str(event.get("title", "")).lower() == "world series champion"
            and event.get("active") is True and event.get("closed") is not True
        ), None)
        pm_event: dict[str, Any] = {}
        pm_markets: list[dict[str, Any]] = []
        if chosen_event is not None:
            detail = await asyncio.to_thread(
                polymarket.client.events.retrieve_by_slug, chosen_event["slug"],
            )
            pm_event = detail.get("event", {}) if isinstance(detail, dict) else {}
            pm_markets = pm_event.get("markets", [])
        candidates: list[dict[str, Any]] = []
        for event in kalshi_events:
            for k_market in event.get("markets", []):
                ticker = str(k_market.get("ticker", ""))
                k_title = str(k_market.get("title") or "")
                k_identity = identify_mlb_world_series_champion(
                    venue="kalshi", contract_id=ticker, season=season,
                    title=k_title, outcome_text=ticker,
                    resolution_rules=str(k_market.get("rules_primary") or "") or None,
                )
                if k_identity.identity_status != "identified":
                    continue
                for pm_market in pm_markets:
                    slug = str(pm_market.get("slug", ""))
                    pm_title = str(pm_market.get("title") or pm_market.get("question") or "")
                    pm_identity = identify_mlb_world_series_champion(
                        venue="polymarket-us", contract_id=slug, season=season,
                        title=pm_title, outcome_text=slug,
                        resolution_rules=str(pm_market.get("description") or "") or None,
                    )
                    identity_comparison = compare_contract_identities(k_identity, pm_identity)
                    if identity_comparison["semantic_match"] != "confirmed":
                        continue
                    try:
                        bbo_result = await asyncio.to_thread(polymarket.client.markets.bbo, slug)
                    except (PolymarketUSError, httpx.HTTPError, RuntimeError,
                            ValueError, KeyError, TypeError, OSError):
                        bbo_result = {}
                    polymarket_received_at = datetime.now(UTC).isoformat()
                    bbo = bbo_result.get("marketData", {}) if isinstance(bbo_result, dict) else {}
                    k_bid = _number(k_market.get("yes_bid_dollars"))
                    k_ask = _number(k_market.get("yes_ask_dollars"))
                    p_bid = _number((bbo.get("bestBid") or {}).get("value"))
                    p_ask = _number((bbo.get("bestAsk") or {}).get("value"))
                    pm_rules = str(pm_market.get("description") or "")
                    kalshi_primary = str(k_market.get("rules_primary") or "")
                    kalshi_secondary = str(k_market.get("rules_secondary") or "")
                    kalshi_rules = "\n".join(
                        part for part in (kalshi_primary, kalshi_secondary) if part
                    )
                    kalshi_fees: dict[str, Any] = {
                        "verified": False,
                        "unknown_fields": ["fee_schedule_version", "maker_taker_assumption", "rounding", "fee_terms"],
                    }
                    try:
                        terms = await kalshi.taker_fee_terms(ticker)
                        from dataclasses import asdict

                        kalshi_fees = {
                            "verified": True,
                            "venue": "kalshi",
                            "schedule_version": terms.schedule_version,
                            "maker_taker_assumption": "taker",
                            "rounding": "Kalshi fee terms implementation; conservative cent rounding",
                            "terms": {key: str(value) for key, value in asdict(terms).items()},
                            "source": "Kalshi series/event API",
                            "unknown_fields": [],
                        }
                    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
                        pass
                    kalshi_depth: dict[str, Any] | None = None
                    try:
                        depth_response = await kalshi.client.get(f"/markets/{ticker}")
                        depth_response.raise_for_status()
                        depth_payload = depth_response.json()
                        depth_market = depth_payload.get("market", {}) if isinstance(depth_payload, dict) else {}
                        kalshi_depth = {
                            "source": "Kalshi public market detail",
                            "yes_bid_size": depth_market.get("yes_bid_size_fp"),
                            "yes_ask_size": depth_market.get("yes_ask_size_fp"),
                        }
                    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
                        pass
                    polymarket_depth: Any = None
                    try:
                        polymarket_depth = await asyncio.to_thread(polymarket.client.markets.book, slug)
                    except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                            KeyError, TypeError, OSError):
                        pass
                    kalshi_size_bid = _number((kalshi_depth or {}).get("yes_bid_size"))
                    kalshi_size_ask = _number((kalshi_depth or {}).get("yes_ask_size"))
                    normalized_kalshi_depth = None
                    if (k_bid is not None and k_ask is not None and kalshi_size_bid is not None
                            and kalshi_size_ask is not None and kalshi_size_bid > 0
                            and kalshi_size_ask > 0):
                        normalized_kalshi_depth = {
                            "yes_bids": [{"price": str(k_bid), "contracts": str(kalshi_size_bid)}],
                            "yes_asks": [{"price": str(k_ask), "contracts": str(kalshi_size_ask)}],
                            "normalization": "Kalshi public market detail fixed-point YES quote sizes",
                        }
                    normalized_pm_depth = _normalize_polymarket_depth(polymarket_depth, slug)
                    k_rule_url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
                    pm_rule_url = f"https://api.polymarket.us/v1/market/slug/{slug}"
                    k_extracted = extract_clause_evidence(
                        "kalshi", ticker, kalshi_rules, k_rule_url,
                        source_field="rules_primary + rules_secondary",
                    )
                    p_extracted = extract_clause_evidence(
                        "polymarket_us", slug, pm_rules, pm_rule_url,
                        source_field="description",
                    )
                    contract_clauses = {}
                    for clause in (name for name in k_extracted if not name.startswith("_")):
                        left, right = k_extracted[clause], p_extracted[clause]
                        contract_clauses[clause] = {
                            "kalshi": {"text": left["text"], "status": left["status"],
                                       "source_evidence": left["citation"]},
                            "polymarket_us": {"text": right["text"], "status": right["status"],
                                              "source_evidence": right["citation"]},
                            "citations": {"kalshi": left["citation"],
                                          "polymarket_us": right["citation"]},
                        }
                    contract_clauses["_source_text"] = {
                        "kalshi": kalshi_rules or None,
                        "polymarket_us": pm_rules or None,
                    }
                    candidates.append({
                        "observed_at": datetime.now(UTC).isoformat(),
                        "status": "candidate_discovered",
                        "team_code": k_identity.outcome_id,
                        "event": str(pm_event.get("title") or chosen_event.get("title")),
                        "year": season,
                        "canonical_identity": {
                            "comparison": identity_comparison,
                            "contracts": [
                                {**k_identity.to_dict(), "active_at_observation": k_market.get("status") == "active"},
                                {**pm_identity.to_dict(), "active_at_observation": pm_market.get("active") is True and pm_market.get("closed") is not True},
                            ],
                        },
                        "contract_clauses": contract_clauses,
                        "kalshi": {
                            "market_id": ticker,
                            "ticker": ticker,
                            "title": k_title,
                            "yes_bid": k_bid,
                            "yes_ask": k_ask,
                            "spread": None if None in (k_bid, k_ask) else round(k_ask - k_bid, 6),
                            "source_timestamp": None,
                            "source_timestamp_semantics": "Kalshi market updated_at is record-level, not a documented YES quote timestamp; only NOEMA receipt time is available.",
                            "quote_observed_at": kalshi_received_at,
                            "fees": kalshi_fees,
                            "raw_depth": kalshi_depth,
                            "normalized_depth": normalized_kalshi_depth,
                            "capacity": "unknown",
                            "book_source": None if kalshi_depth is None else kalshi_depth.get("source"),
                            "resolution_rules": kalshi_rules or None,
                            "rule_source_url": k_rule_url,
                            "market_status_at_observation": k_market.get("status"),
                            "settlement_rule_available": bool(kalshi_rules),
                        },
                        "polymarket_us": {
                            "market_id": slug,
                            "ticker": slug,
                            "title": pm_title,
                            "yes_bid": p_bid,
                            "yes_ask": p_ask,
                            "spread": None if None in (p_bid, p_ask) else round(p_ask - p_bid, 6),
                            "source_timestamp": None,
                            "source_timestamp_semantics": "Official Polymarket US BBO response schema provides no quote-origin timestamp; only NOEMA receipt time is available.",
                            "quote_observed_at": polymarket_received_at,
                            "fees": {"verified": False,
                                     "venue": "polymarket_us",
                                     "source": "Official Polymarket US market-detail response; feeCoefficient is retained as raw venue evidence, but official documentation does not establish its taker-fee formula, rounding, or applicability for this paper fill.",
                                     "raw_fee_coefficient": pm_market.get("feeCoefficient"),
                                     "maker_taker_assumption": None,
                                     "rounding": None,
                                     "fee_terms": None,
                                     "unknown_fields": ["fee_schedule_version", "maker_taker_assumption", "rounding", "fee_terms"]},
                            "raw_depth": polymarket_depth,
                            "normalized_depth": normalized_pm_depth,
                            "capacity": "unknown",
                            "book_source": "Polymarket US public market book" if polymarket_depth else None,
                            "resolution_rules": pm_rules or None,
                            "rule_source_url": pm_rule_url,
                            "market_status_at_observation": {"active": pm_market.get("active"), "closed": pm_market.get("closed")},
                            "settlement_rule_available": bool(pm_rules),
                            "has_postponement_or_cancellation_clause": (
                                "postpon" in pm_rules.lower() or "cancel" in pm_rules.lower()
                            ),
                        },
                        "settlement_difference": (
                            "Polymarket US publishes postponement/cancellation fair-price contingencies; "
                            "Kalshi's primary rule text states the team-win resolution condition. "
                            "Full venue-rule equivalence is unverified, so execution value is not comparable yet."
                        ),
                        "unadjusted_yes_ask_difference": (
                            None if None in (p_ask, k_ask) else round(p_ask - k_ask, 6)
                        ),
                        "executable_edge": None,
                        "evidence_citations": [
                            {"source": "Kalshi official API", "identifier": f"event series KXMLB; market {ticker}"},
                            {"source": "Polymarket US official SDK/API", "identifier": f"event {chosen_event.get('slug')}; market {slug}"},
                        ],
                        "reason": (
                            "The same team and championship outcome appear on both venues, but "
                            "venue-specific postponement/cancellation and settlement terms have "
                            "not been proven equivalent. Prices are descriptive, not an arbitrage claim."
                        ),
                    })
                    # Keep collection bounded so one dashboard refresh does
                    # not fan out across the full event. Every pair in this
                    # fixed sample is registered, including incomplete ones.
                    if len(candidates) >= 5:
                        break
                if len(candidates) >= 5:
                    break
            if len(candidates) >= 5:
                break
        tesla_candidate = await _discover_tesla_threshold_candidate(kalshi, polymarket)
        if tesla_candidate is not None:
            candidates.append(tesla_candidate)
        if not candidates:
            return {"status": "no_current_candidate", "matches": [], "reason": "No same-outcome contract with current quotes was found on both venues."}
        candidates.sort(key=lambda item: (
            item["unadjusted_yes_ask_difference"] is None,
            abs(item["unadjusted_yes_ask_difference"])
            if item["unadjusted_yes_ask_difference"] is not None else float("inf"),
        ))
        for candidate in candidates[:5]:
            candidate["experiment_evaluation"] = evaluate_candidate(candidate)
        return {
            "status": "candidate_only",
            "matches": candidates[:5],
            "reason": "A real overlapping outcome is visible; unsupported rules, source freshness, fees, or normalized depth block paper eligibility.",
        }
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
        return {"status": "unavailable", "matches": [], "reason": "Cross-venue candidate discovery failed safely."}
    finally:
        await kalshi.close()
        polymarket.close()


def _retain_last_successful_account(current: dict[str, Any], previous: dict[str, Any] | None) -> None:
    account = current.get("account") if isinstance(current.get("account"), dict) else {}
    if account.get("status") not in {"authentication_failed", "authentication_or_read_failed"}:
        return
    old_account = (previous or {}).get("account") if isinstance(previous, dict) else None
    if not isinstance(old_account, dict) or old_account.get("status") not in {
        "authenticated_read_only", "stale_authenticated_read_only",
    }:
        return
    retained = copy.deepcopy(old_account)
    retained.pop("_persisted_records", None)
    retained["status"] = "stale_authenticated_read_only"
    retained["freshness"] = "STALE"
    retained["collection_error_type"] = account.get("error_type") or "read_failed"
    retained["collection_error"] = (account.get("account_read") or {}).get("reason")
    metrics = retained.get("account_metrics") or (retained.get("capital") or {}).get("metrics") or {}
    for metric in metrics.values():
        if isinstance(metric, dict):
            metric["freshness"] = "STALE"
            metric["reconciliation_state"] = "STALE_OBSERVATION"
    current["account"] = retained
    current["account_collection_status"] = "DEGRADED · LAST SUCCESSFUL OBSERVATION RETAINED"


def _stamp_metric_metadata(venue: dict[str, Any]) -> None:
    account = venue.get("account") if isinstance(venue.get("account"), dict) else {}
    observed_at = account.get("observed_at")
    stale = account.get("status") == "stale_authenticated_read_only"
    account.setdefault("last_success_at", observed_at)
    account.setdefault("freshness", "STALE" if stale else "LIVE")
    coverage = account.get("history_coverage") if isinstance(account.get("history_coverage"), dict) else {}
    groups = [account.get("account_metrics"), (account.get("capital") or {}).get("metrics")]
    for metrics in groups:
        if not isinstance(metrics, dict):
            continue
        for metric in metrics.values():
            if not isinstance(metric, dict):
                continue
            metric.setdefault("source", venue.get("venue"))
            metric.setdefault("observed_at", observed_at)
            metric.setdefault("freshness", "STALE" if stale else "LIVE")
            metric.setdefault("coverage", copy.deepcopy(coverage))
            metric.setdefault("reconciliation_state", (
                "STALE_OBSERVATION" if stale else
                "RECONCILED" if metric.get("value") is not None and metric.get("reason") is None else
                "UNAVAILABLE"
            ))



async def _discover_tesla_threshold_candidate(
    kalshi: KalshiVenue, polymarket: PolymarketUSVenue,
) -> dict[str, Any] | None:
    """Resolve the live Tesla Q3 delivery threshold only via strict source IDs."""
    try:
        k_response = await kalshi.client.get(
            "/events", params={"series_ticker": "KXTSLA", "status": "open",
                                "limit": 100, "with_nested_markets": "true"},
        )
        k_response.raise_for_status()
        k_events = k_response.json().get("events", [])
        search = await asyncio.to_thread(
            polymarket.client.search.query,
            {"query": "Tesla Q3 Total Deliveries"},
        )
        pm_event_ref = next((
            event for event in (search.get("events", []) if isinstance(search, dict) else [])
            if event.get("slug") == "tsla-dlvrs-2026-q3-above"
            and event.get("active") is True and event.get("closed") is not True
        ), None)
        if pm_event_ref is None:
            return None
        detail = await asyncio.to_thread(
            polymarket.client.events.retrieve_by_slug, pm_event_ref["slug"],
        )
        pm_event = detail.get("event", {}) if isinstance(detail, dict) else {}
        pm_markets = pm_event.get("markets", [])
        for event in k_events:
            if event.get("event_ticker") != "KXTSLA-26OCTDELIV":
                continue
            for k_market in event.get("markets", []):
                k_ticker = str(k_market.get("ticker") or "")
                k_identity = identify_tesla_q3_deliveries_threshold(
                    venue="kalshi", contract_id=k_ticker,
                    resolution_rules=str(k_market.get("rules_primary") or ""),
                )
                if k_identity.identity_status != "identified":
                    continue
                for pm_market in pm_markets:
                    slug = str(pm_market.get("slug") or "")
                    pm_identity = identify_tesla_q3_deliveries_threshold(
                        venue="polymarket-us", contract_id=slug,
                        resolution_rules=str(pm_market.get("description") or ""),
                    )
                    comparison = compare_contract_identities(k_identity, pm_identity)
                    if comparison["semantic_match"] != "confirmed":
                        continue
                    bbo_response = await asyncio.to_thread(polymarket.client.markets.bbo, slug)
                    bbo = bbo_response.get("marketData", {}) if isinstance(bbo_response, dict) else {}
                    if bbo.get("marketSlug") != slug:
                        continue
                    k_bid = _number(k_market.get("yes_bid_dollars"))
                    k_ask = _number(k_market.get("yes_ask_dollars"))
                    pm_bid = _number((bbo.get("bestBid") or {}).get("value"))
                    pm_ask = _number((bbo.get("bestAsk") or {}).get("value"))
                    if None in (k_bid, k_ask, pm_bid, pm_ask):
                        continue
                    observed_at = datetime.now(UTC).isoformat()
                    settlement = assess_settlement_equivalence(
                        SettlementTerms(
                            evidence_ref=f"kalshi:{k_ticker}:rules_primary",
                            evidence_sha256=k_identity.settlement_rule_sha256,
                            rule_version=(f"rules-sha256:{k_identity.settlement_rule_sha256}"
                                          if k_identity.settlement_rule_sha256 else None),
                            observed_at=observed_at,
                        ),
                        SettlementTerms(
                            evidence_ref=f"polymarket-us:{slug}:description",
                            evidence_sha256=pm_identity.settlement_rule_sha256,
                            rule_version=(f"rules-sha256:{pm_identity.settlement_rule_sha256}"
                                          if pm_identity.settlement_rule_sha256 else None),
                            observed_at=observed_at,
                        ),
                    )
                    economics = assess_economic_comparability(
                        EconomicQuote(
                            denomination="USD per binary contract", bid=str(k_bid), ask=str(k_ask),
                            quote_timestamp=observed_at,
                            evidence_ref=f"kalshi:{k_ticker}:market_quote", observed_at=observed_at,
                        ),
                        EconomicQuote(
                            denomination="USD per binary contract", bid=str(pm_bid), ask=str(pm_ask),
                            quote_timestamp=observed_at,
                            evidence_ref=f"polymarket-us:{slug}:bbo", observed_at=observed_at,
                        ), settlement_status=settlement["status"],
                    )
                    comparison["settlement_equivalence"] = settlement["status"]
                    comparison["economic_comparability"] = economics["status"]
                    comparison["levels"]["settlement_equivalence"] = settlement["status"]
                    comparison["levels"]["economic_comparability"] = economics["status"]
                    comparison["levels"]["executable_comparability"] = "unavailable"
                    return {
                        "observed_at": observed_at,
                        "status": "canonical_numeric_threshold_match_settlement_unverified",
                        "event": "Tesla Q3 2026 total deliveries",
                        "proposition_label": f"deliveries > {k_ticker.rsplit('-', 1)[-1]}",
                        "canonical_identity": {
                            "comparison": comparison,
                            "contracts": [k_identity.to_dict(), pm_identity.to_dict()],
                        },
                        "settlement_assessment": settlement,
                        "economic_comparability": economics,
                        "kalshi": {
                            "market_id": k_ticker, "title": str(k_market.get("title") or ""),
                            "yes_bid": k_bid, "yes_ask": k_ask,
                            "spread": round(k_ask - k_bid, 6),
                        },
                        "polymarket_us": {
                            "market_id": slug, "title": str(pm_market.get("title") or ""),
                            "yes_bid": pm_bid, "yes_ask": pm_ask,
                            "spread": round(pm_ask - pm_bid, 6),
                        },
                        "unadjusted_yes_ask_difference": round(pm_ask - k_ask, 6),
                        "executable_edge": None,
                        "reason": (
                            "Exact structured thresholds match across both active venue contracts; "
                            "publication, revisions, expiry, fees, and executable depth are not reconciled."
                        ),
                    }
        return None
    except (httpx.HTTPError, PolymarketUSError, RuntimeError, ValueError,
            KeyError, TypeError, OSError):
        return None



def _load_registered_quote_pair() -> list[dict[str, Any]] | None:
    runtime_path = os.getenv("NOEMA_DB_PATH", "data/noema.db")
    return _load_registered_pair_rows((console_state_db_path(runtime_path), runtime_path)) or None


async def _refresh_registered_pair(previous: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh quotes for a registered pair without title/rule identity work."""
    identities = previous.get("canonical_identity", {}).get("contracts", [])
    if len(identities) != 2:
        return None
    kalshi_identity = next((item for item in identities if item.get("venue") == "kalshi"), None)
    pm_identity = next((item for item in identities if item.get("venue") == "polymarket-us"), None)
    if not kalshi_identity or not pm_identity:
        return None
    kalshi = KalshiVenue(KalshiConfig(
        environment="production", allow_live_orders=False, master_halt=True,
    ))
    polymarket = PolymarketUSVenue()
    try:
        market_response, bbo_response = await asyncio.gather(
            kalshi.client.get(f"/markets/{kalshi_identity['contract_id']}"),
            asyncio.to_thread(polymarket.client.markets.bbo, pm_identity["contract_id"]),
        )
        market_response.raise_for_status()
        market_payload = market_response.json()
        k_market = market_payload.get("market", market_payload)
        bbo = bbo_response.get("marketData", {}) if isinstance(bbo_response, dict) else {}
        if bbo.get("marketSlug") != pm_identity["contract_id"]:
            return None
        k_bid, k_ask = _number(k_market.get("yes_bid_dollars")), _number(k_market.get("yes_ask_dollars"))
        p_bid = _number((bbo.get("bestBid") or {}).get("value"))
        p_ask = _number((bbo.get("bestAsk") or {}).get("value"))
        if None in (k_bid, k_ask, p_bid, p_ask):
            return None
        observed_at = datetime.now(UTC).isoformat()
        refreshed = dict(previous)
        refreshed.update(observed_at=observed_at, quote_path="fast_quote_refresh")
        refreshed["kalshi"] = {**previous.get("kalshi", {}), "yes_bid": k_bid,
                                "yes_ask": k_ask, "spread": round(k_ask-k_bid, 6)}
        refreshed["polymarket_us"] = {**previous.get("polymarket_us", {}), "yes_bid": p_bid,
                                      "yes_ask": p_ask, "spread": round(p_ask-p_bid, 6)}
        refreshed["unadjusted_yes_ask_difference"] = round(p_ask-k_ask, 6)
        # Update only quote observation fields. Venue quote source timestamps,
        # fee schedules, executable depth, and capital lock remain unknown.
        refreshed["economic_comparability"] = assess_economic_comparability(
            EconomicQuote(denomination="USD per binary contract", bid=str(k_bid), ask=str(k_ask),
                          quote_timestamp=observed_at, evidence_ref=f"kalshi:{kalshi_identity['contract_id']}:quote",
                          observed_at=observed_at),
            EconomicQuote(denomination="USD per binary contract", bid=str(p_bid), ask=str(p_ask),
                          quote_timestamp=observed_at, evidence_ref=f"polymarket-us:{pm_identity['contract_id']}:bbo",
                          observed_at=observed_at),
            settlement_status=refreshed["settlement_assessment"]["status"],
        )
        return refreshed
    except (httpx.HTTPError, PolymarketUSError, RuntimeError, ValueError,
            KeyError, TypeError, OSError):
        return None
    finally:
        await kalshi.close()
        polymarket.close()



def _number(value: Any) -> float | None:
    number = _decimal(value)
    return None if number is None else float(number)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _decimal_sum(values: list[Any], *, empty_is_zero: bool = False) -> Decimal | None:
    if not values:
        return Decimal(0) if empty_is_zero else None
    parsed = [_decimal(value) for value in values]
    if any(value is None for value in parsed):
        return None
    return sum((value for value in parsed if value is not None), Decimal(0))


def _capital_metric(
    value: Decimal | str | None, provenance: str, reason: str | None = None,
    *, source_ids: list[str] | None = None, components: dict[str, Any] | None = None,
) -> dict[str, Any]:
    serialized = None if value is None else format(value, "f") if isinstance(value, Decimal) else str(value)
    return {"value": serialized, "provenance": provenance, "reason": reason,
            "source_ids": source_ids or [], "components": components or {}}


def _usd_amount(value: Any) -> str | None:
    if isinstance(value, dict):
        if str(value.get("currency", "")).upper() != "USD":
            return None
        value = value.get("value")
    number = _decimal(value)
    return None if number is None else format(number.normalize(), "f")


def _kalshi_balance_updated_at(value: Any) -> str | None:
    """Normalize Kalshi's official Unix-seconds balance timestamp."""
    timestamp = _decimal(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _polymarket_balance_activities(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("activities", []) if isinstance(payload, dict) else []
    result: list[dict[str, Any]] = []
    allowed = {"ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT",
               "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL", "ACTIVITY_TYPE_TRANSFER"}
    for activity in rows:
        if not isinstance(activity, dict) or activity.get("type") not in allowed:
            continue
        change = activity.get("accountBalanceChange")
        transactions = change.get("transactions", []) if isinstance(change, dict) else []
        for transaction in transactions:
            if isinstance(transaction, dict):
                result.append({"transaction_id": transaction.get("transactionId"),
                               "activity_type": activity.get("type"),
                               "status": transaction.get("status"),
                               "amount_usd": _usd_amount(transaction.get("amount")),
                               "created_at": transaction.get("createTime"),
                               "updated_at": transaction.get("updateTime")})
    return result


def _polymarket_positions(payload: Any) -> list[dict[str, Any]]:
    """Project only documented account-position fields from the official SDK."""
    raw = payload.get("positions") if isinstance(payload, dict) else None
    records = list(raw.values()) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    result = []
    for row in records:
        if not isinstance(row, dict):
            continue
        market = row.get("marketMetadata") if isinstance(row.get("marketMetadata"), dict) else {}
        result.append({
            "ticker": market.get("slug") if isinstance(market.get("slug"), str) else None,
            "title": market.get("title") if isinstance(market.get("title"), str) else None,
            "outcome": market.get("outcome") if isinstance(market.get("outcome"), str) else None,
            "position_fp": row.get("netPositionDecimal", row.get("netPosition")),
            "available_fp": row.get("qtyAvailableDecimal", row.get("qtyAvailable")),
            "realized_pnl_dollars": _usd_amount(row.get("realized")),
            "market_value_usd": _usd_amount(row.get("cashValue")),
            "cost_basis_usd": _usd_amount(row.get("cost")),
            "fees_paid_dollars": _usd_amount(row.get("fees")),
            "last_updated_ts": row.get("updateTime"),
        })
    return result


def _polymarket_fills(payload: Any) -> list[dict[str, Any]]:
    records = payload.get("activities", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        return []
    result = []
    for row in records:
        if not isinstance(row, dict) or row.get("type") != "ACTIVITY_TYPE_TRADE":
            continue
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        fill_id = trade.get("id")
        if not isinstance(fill_id, str) or not fill_id:
            continue
        price = _usd_amount(trade.get("price"))
        result.append({
            "fill_id": fill_id,
            "ticker": trade.get("marketSlug") if isinstance(trade.get("marketSlug"), str) else None,
            "count_fp": trade.get("qty"), "side": None, "fee_cost": None,
            "price_usd": price, "cost_basis_usd": _usd_amount(trade.get("costBasis")),
            "realized_pnl_usd": _usd_amount(trade.get("realizedPnl")),
            "created_time": trade.get("createTime"), "state": trade.get("state"),
            "title": (trade.get("marketMetadata") or {}).get("title") if isinstance(trade.get("marketMetadata"), dict) else None,
        })
    return result


def _polymarket_resolutions(payload: Any) -> list[dict[str, Any]]:
    activities = payload.get("activities", []) if isinstance(payload, dict) else []
    result = []
    for row in activities if isinstance(activities, list) else []:
        if not isinstance(row, dict) or row.get("type") != "ACTIVITY_TYPE_POSITION_RESOLUTION":
            continue
        resolution = row.get("positionResolution") if isinstance(row.get("positionResolution"), dict) else {}
        trade_id = resolution.get("tradeId")
        if not trade_id:
            continue
        result.append({
            "trade_id": str(trade_id), "ticker": resolution.get("marketSlug"),
            "side": resolution.get("side"), "created_time": resolution.get("updateTime"),
            "before_position": resolution.get("beforePosition"),
            "after_position": resolution.get("afterPosition"),
        })
    return result


def _polymarket_orders(payload: Any) -> list[dict[str, Any]]:
    records = payload.get("orders", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        return []
    return [{
        "order_id": row.get("id"), "ticker": row.get("marketSlug"),
        "status": row.get("status"), "side": row.get("side"),
        "fill_count_fp": row.get("cumQuantity"), "remaining_count_fp": row.get("leavesQuantity"),
        "created_time": row.get("createTime"), "last_update_time": row.get("updateTime"),
    } for row in records if isinstance(row, dict)]


def _polymarket_position_capital(
    positions: list[dict[str, Any]], position_payload: Any, fills: list[dict[str, Any]],
    *, activity_complete: bool = False, resolution_rows: list[dict[str, Any]] | None = None,
    activity_incomplete_reason: str | None = None,
) -> dict[str, Any]:
    raw = position_payload.get("positions") if isinstance(position_payload, dict) else None
    has_more = bool(position_payload.get("nextCursor")) or position_payload.get("eof") is False if isinstance(position_payload, dict) else False
    if isinstance(raw, (dict, list)) and len(raw) > 100:
        has_more = True

    def total(rows: list[dict[str, Any]], field: str, *, signed: bool = False) -> str | None:
        if not rows:
            return None
        values = [_number(row.get(field)) for row in rows]
        if any(value is None or (not signed and value < 0) for value in values):
            return None
        return format(sum((Decimal(str(value)) for value in values), Decimal(0)).normalize(), "f")

    volume_values = []
    for row in fills:
        quantity, price = _decimal(row.get("count_fp")), _decimal(row.get("price_usd"))
        volume_values.append(None if quantity is None or price is None else abs(quantity * price))
    resolutions = {str(row.get("trade_id")): row for row in (resolution_rows or []) if row.get("trade_id")}
    realized_values: list[Decimal | None] = []
    for row in fills:
        realized = _decimal(row.get("realized_pnl_usd"))
        resolution = resolutions.get(str(row.get("fill_id")))
        if realized is None and resolution:
            before = resolution.get("before_position") if isinstance(resolution.get("before_position"), dict) else {}
            after = resolution.get("after_position") if isinstance(resolution.get("after_position"), dict) else {}
            before_value, after_value = _usd_amount(before.get("realized")), _usd_amount(after.get("realized"))
            if before_value is not None and after_value is not None:
                realized = _decimal(after_value) - _decimal(before_value)
        realized_values.append(realized)
    exposure_values = [_decimal(row.get("market_value_usd")) for row in positions]
    unrealized_values = [
        None if _decimal(row.get("market_value_usd")) is None or _decimal(row.get("cost_basis_usd")) is None
        else _decimal(row.get("market_value_usd")) - _decimal(row.get("cost_basis_usd"))
        for row in positions
    ]
    fee_values = [_decimal(row.get("fees_paid_dollars")) for row in positions]
    no_open_positions = isinstance(raw, (dict, list)) and not raw and not has_more
    realized_complete = activity_complete and all(value is not None for value in realized_values)
    volume_complete = activity_complete and all(value is not None for value in volume_values)
    exposure_complete = isinstance(raw, (dict, list)) and not has_more and all(value is not None for value in exposure_values)
    unrealized_complete = no_open_positions or (
        isinstance(raw, (dict, list)) and not has_more and all(value is not None for value in unrealized_values)
    )
    pnl = sum((value for value in realized_values if value is not None), Decimal(0)) if realized_complete else None
    volume = sum((value for value in volume_values if value is not None), Decimal(0)) if volume_complete else None
    exposure = sum((abs(value) for value in exposure_values if value is not None), Decimal(0)) if exposure_complete else None
    unrealized = sum((value for value in unrealized_values if value is not None), Decimal(0)) if unrealized_complete and not no_open_positions else Decimal(0) if no_open_positions else None
    fee_subtotal = sum((value for value in fee_values if value is not None), Decimal(0)) if all(value is not None for value in fee_values) else None
    unresolved_realized = sum(value is None for value in realized_values)
    resolution_links = sum(str(row.get("fill_id")) in resolutions for row in fills if row.get("fill_id"))
    metrics = {
        "exposure": _capital_metric(exposure, "VENUE_REPORTED" if exposure is not None else "UNAVAILABLE",
            None if exposure is not None else "Position pagination is incomplete or an active position has no USD cash value.",
            source_ids=[str(row.get("ticker")) for row in positions if row.get("ticker")]),
        "unrealized_pnl": _capital_metric(unrealized, "DERIVED_FROM_LIVE_RECORDS" if unrealized is not None else "UNAVAILABLE",
            None if unrealized is not None else "Open-position cash value or cost basis is missing.",
            source_ids=[str(row.get("ticker")) for row in positions if row.get("ticker")],
            components={"basis": "sum(open position cashValue - cost)"}),
        "realized_pnl": _capital_metric(pnl, "VENUE_REPORTED" if pnl is not None else "UNAVAILABLE",
            None if pnl is not None else (activity_incomplete_reason or "Activity pagination is incomplete." if not activity_complete else
                f"{unresolved_realized} of {len(fills)} trade activities omit realizedPnl; "
                f"{resolution_links} are linked to position-resolution records, which is insufficient to reconcile every trade."),
            source_ids=[str(row.get("fill_id")) for row in fills if row.get("fill_id")]
                + [str(row.get("trade_id")) for row in (resolution_rows or []) if row.get("trade_id")],
            components={"basis": "sum of official trade realizedPnl plus linked position-resolution realized deltas",
                        "trade_count": len(fills), "unresolved_trade_count": unresolved_realized,
                        "position_resolution_count": len(resolution_rows or []), "linked_resolution_count": resolution_links}),
        "volume": _capital_metric(volume, "DERIVED_FROM_LIVE_RECORDS" if volume is not None else "UNAVAILABLE",
            None if volume is not None else (activity_incomplete_reason or "Activity pagination is incomplete." if not activity_complete else "At least one trade is missing USD price or quantity."),
            source_ids=[str(row.get("fill_id")) for row in fills if row.get("fill_id")],
            components={"basis": "sum(abs(trade price × quantity))", "currency": "USD"}),
        "fees": _capital_metric(None, "UNAVAILABLE",
            "Polymarket US trade activity exposes no per-trade fee amount; active-position fee totals omit closed positions.",
            source_ids=[str(row.get("ticker")) for row in positions if row.get("ticker")],
            components={"open_position_fee_subtotal_usd": None if fee_subtotal is None else format(fee_subtotal, "f")}),
    }
    return {
        "observed_volume_usd": None if volume is None else format(volume, "f"),
        "reported_realized_pnl_usd": None if pnl is None else format(pnl, "f"),
        # Active-position fees are a subtotal only; historical/closed-position
        # trade fees are not exposed by this official activity schema.
        "reported_fees_usd": None,
        "reported_exposure_usd": None if exposure is None else format(exposure, "f"),
        "metrics": metrics,
        "pagination_complete": activity_complete and isinstance(raw, (dict, list)) and not has_more,
        "position_rows": len(raw) if isinstance(raw, (dict, list)) else None,
        "more_positions": has_more,
        "scope": "Authenticated live positions and fully paginated activity",
        "positions": positions,
    }


def _normalize_polymarket_depth(payload: Any, slug: str) -> dict[str, Any] | None:
    """Normalize official L2 price/quantity rows; malformed books stay unknown."""
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("marketSlug") != slug:
        return None

    def levels(name: str, *, descending: bool) -> list[dict[str, str]] | None:
        raw = data.get(name)
        if not isinstance(raw, list) or not raw:
            return None
        normalized: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                return None
            price_data = item.get("px")
            price = _number(price_data.get("value") if isinstance(price_data, dict) else price_data)
            quantity = _number(item.get("qty"))
            if (price is None or quantity is None or not 0 < price < 1 or quantity <= 0):
                return None
            normalized.append({
                "price": format(Decimal(str(price)).normalize(), "f"),
                "contracts": format(Decimal(str(quantity)).normalize(), "f"),
            })
        prices = [Decimal(level["price"]) for level in normalized]
        if prices != sorted(prices, reverse=descending) or len(prices) != len(set(prices)):
            return None
        return normalized

    bids = levels("bids", descending=True)
    asks = levels("offers", descending=False)
    if not bids or not asks:
        return None
    return {"yes_bids": bids, "yes_asks": asks,
            "normalization": "Polymarket US official L2 price/qty levels"}


def _normalize_polymarket_depth(payload: Any, slug: str) -> dict[str, Any] | None:
    """Normalize official L2 price/quantity rows; malformed books stay unknown."""
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("marketSlug") != slug:
        return None

    def levels(name: str, *, descending: bool) -> list[dict[str, str]] | None:
        raw = data.get(name)
        if not isinstance(raw, list) or not raw:
            return None
        normalized: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                return None
            price_data = item.get("px")
            price = _number(price_data.get("value") if isinstance(price_data, dict) else price_data)
            quantity = _number(item.get("qty"))
            if price is None or quantity is None or not 0 < price < 1 or quantity <= 0:
                return None
            normalized.append({
                "price": format(Decimal(str(price)).normalize(), "f"),
                "contracts": format(Decimal(str(quantity)).normalize(), "f"),
            })
        prices = [Decimal(level["price"]) for level in normalized]
        if prices != sorted(prices, reverse=descending) or len(prices) != len(set(prices)):
            return None
        return normalized

    bids, asks = levels("bids", descending=True), levels("offers", descending=False)
    if not bids or not asks:
        return None
    return {"yes_bids": bids, "yes_asks": asks,
            "normalization": "Polymarket US official L2 price/qty levels"}


def _fixed_lifecycle_pairs(
    path: str, *, fallback_paths: tuple[str, ...] = (), limit: int = 5,
) -> set[tuple[str, str]]:
    """Read the already-registered lifecycle cohort without enrolling discoveries."""
    if not 1 <= limit <= 5:
        raise ValueError("lifecycle cohort limit must be 1..5")
    pairs: set[tuple[str, str]] = set()
    for candidate_path in (path, *fallback_paths):
        db = Path(candidate_path)
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if "canonical_pair_observations" not in tables:
                    continue
                rows = conn.execute(
                    "SELECT observation_json FROM canonical_pair_observations "
                    "ORDER BY id DESC LIMIT 500"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for (payload,) in rows:
            try:
                observation = json.loads(payload)
                evaluation = observation.get("experiment_evaluation")
                contracts = (observation.get("canonical_identity") or {}).get("contracts", [])
                if not isinstance(evaluation, dict) or not isinstance(contracts, list):
                    continue
                by_venue = {str(item.get("venue")): item for item in contracts if isinstance(item, dict)}
                kalshi = by_venue.get("kalshi", {}).get("contract_id")
                polymarket = by_venue.get("polymarket-us", {}).get("contract_id")
                if kalshi and polymarket:
                    pairs.add((str(kalshi), str(polymarket)))
                    if len(pairs) >= limit:
                        return pairs
            except (ValueError, TypeError, AttributeError):
                continue
    return pairs


def _load_registered_pair_rows(paths: tuple[str, ...]) -> list[dict[str, Any]]:
    """Read registered pairs from durable console history and worker snapshots."""
    registered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        database = Path(path)
        if not database.is_file():
            continue
        try:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if "canonical_pair_observations" not in tables:
                    continue
                rows = conn.execute(
                    """SELECT canonical_proposition_id, observation_json
                       FROM canonical_pair_observations WHERE semantic_status='confirmed'
                       ORDER BY id DESC LIMIT 100""",
                ).fetchall()
            for proposition_id, payload in rows:
                if proposition_id in seen:
                    continue
                value = json.loads(payload)
                identities = value.get("canonical_identity", {}).get("contracts", [])
                venues = {item.get("venue") for item in identities if isinstance(item, dict)}
                if venues == {"kalshi", "polymarket-us"}:
                    registered.append(value)
                    seen.add(proposition_id)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            continue
    return registered


async def _lifecycle_evidence(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh evidence for a registered pair without rerunning identity resolution."""
    contracts = (candidate.get("canonical_identity") or {}).get("contracts", [])
    by_venue = {str(item.get("venue")): item for item in contracts if isinstance(item, dict)}
    kalshi_identity = by_venue.get("kalshi")
    pm_identity = by_venue.get("polymarket-us")
    if not kalshi_identity or not pm_identity:
        return None
    ticker = str(kalshi_identity.get("contract_id") or "")
    slug = str(pm_identity.get("contract_id") or "")
    if not ticker or not slug:
        return None

    kalshi = KalshiVenue(KalshiConfig(
        environment="production", allow_live_orders=False, master_halt=True,
    ))
    polymarket = PolymarketUSVenue()
    kalshi_market: dict[str, Any] = {}
    pm_market: dict[str, Any] = {}
    bbo: dict[str, Any] = {}
    k_depth_raw: dict[str, Any] | None = None
    p_depth_raw: dict[str, Any] | None = None
    k_fees: dict[str, Any] = {"verified": False, "venue": "kalshi",
                              "unknown_fields": ["schedule_version", "maker_taker_assumption", "rounding", "fee_terms"]}
    pm_fees: dict[str, Any] = {"verified": False, "venue": "polymarket_us",
                               "unknown_fields": ["schedule_version", "maker_taker_assumption", "rounding", "fee_terms"]}
    k_received = p_received = datetime.now(UTC).isoformat()
    try:
        try:
            response = await kalshi.client.get(f"/markets/{ticker}")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("market"), dict):
                kalshi_market = payload["market"]
            k_received = datetime.now(UTC).isoformat()
            k_depth_raw = {"source": "Kalshi public market detail",
                           "yes_bid_size": kalshi_market.get("yes_bid_size_fp"),
                           "yes_ask_size": kalshi_market.get("yes_ask_size_fp")}
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
        try:
            terms = await kalshi.taker_fee_terms(ticker)
            from dataclasses import asdict
            k_fees = {"verified": True, "venue": "kalshi",
                      "schedule_version": terms.schedule_version,
                      "maker_taker_assumption": "taker",
                      "rounding": "Kalshi fee terms implementation; conservative cent rounding",
                      "terms": {key: str(value) for key, value in asdict(terms).items()},
                      "source": "Kalshi official series/event API", "unknown_fields": []}
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
        try:
            pm_market = await polymarket.market_by_slug(slug)
        except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                KeyError, TypeError, OSError):
            pass
        try:
            result = await asyncio.to_thread(polymarket.client.markets.bbo, slug)
            bbo = result.get("marketData", {}) if isinstance(result, dict) else {}
            p_received = datetime.now(UTC).isoformat()
        except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                KeyError, TypeError, OSError):
            pass
        try:
            p_depth_raw = await asyncio.to_thread(polymarket.client.markets.book, slug)
        except (PolymarketUSError, httpx.HTTPError, RuntimeError, ValueError,
                KeyError, TypeError, OSError):
            pass
    finally:
        await kalshi.close()
        polymarket.close()

    k_bid, k_ask = _number(kalshi_market.get("yes_bid_dollars")), _number(kalshi_market.get("yes_ask_dollars"))
    p_bid = _number((bbo.get("bestBid") or {}).get("value"))
    p_ask = _number((bbo.get("bestAsk") or {}).get("value"))
    k_depth = None
    k_bid_size = _number((k_depth_raw or {}).get("yes_bid_size"))
    k_ask_size = _number((k_depth_raw or {}).get("yes_ask_size"))
    if all(value is not None and value > 0 for value in (k_bid, k_ask, k_bid_size, k_ask_size)):
        k_depth = {"yes_bids": [{"price": str(k_bid), "contracts": str(k_bid_size)}],
                   "yes_asks": [{"price": str(k_ask), "contracts": str(k_ask_size)}],
                   "normalization": "Kalshi public market detail fixed-point YES quote sizes"}
    pm_depth = _normalize_polymarket_depth(p_depth_raw, slug)
    k_rules = "\n".join(str(kalshi_market.get(key) or "") for key in ("rules_primary", "rules_secondary")
                         if kalshi_market.get(key))
    pm_rules = str(pm_market.get("description") or "")
    k_url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
    pm_url = f"https://api.polymarket.us/v1/market/slug/{slug}"
    k_clauses = extract_clause_evidence("kalshi", ticker, k_rules, k_url,
                                        source_field="rules_primary + rules_secondary")
    pm_clauses = extract_clause_evidence("polymarket_us", slug, pm_rules, pm_url,
                                         source_field="description")
    clauses: dict[str, Any] = {}
    for name in (key for key in k_clauses if not key.startswith("_")):
        left, right = k_clauses[name], pm_clauses[name]
        clauses[name] = {
            "kalshi": {"text": left["text"], "status": left["status"], "source_evidence": left["citation"]},
            "polymarket_us": {"text": right["text"], "status": right["status"], "source_evidence": right["citation"]},
            "citations": {"kalshi": left["citation"], "polymarket_us": right["citation"]},
        }
    clauses["_source_text"] = {"kalshi": k_rules or None, "polymarket_us": pm_rules or None}
    comparison = dict((candidate.get("canonical_identity") or {}).get("comparison") or {})
    identity = dict(candidate.get("canonical_identity") or {})
    identity["comparison"] = comparison
    identity["contracts"] = [
        {**kalshi_identity, "venue": "kalshi", "active_at_observation": kalshi_market.get("status") == "active"},
        {**pm_identity, "venue": "polymarket-us",
         "active_at_observation": pm_market.get("active") is True and pm_market.get("closed") is not True},
    ]
    return {
        **candidate, "canonical_identity": identity, "contract_clauses": clauses,
        "kalshi": {"market_id": ticker, "ticker": ticker, "title": kalshi_market.get("title"),
                   "yes_bid": k_bid, "yes_ask": k_ask, "quote_observed_at": k_received,
                   "source_timestamp": None,
                   "source_timestamp_semantics": "Kalshi market updated_at is record-level, not a documented YES quote timestamp; only NOEMA receipt time is available.",
                   "fees": k_fees, "raw_depth": k_depth_raw, "normalized_depth": k_depth,
                   "capacity": "unknown", "book_source": (k_depth_raw or {}).get("source"),
                   "resolution_rules": k_rules or None, "rule_source_url": k_url,
                   "market_status_at_observation": kalshi_market.get("status")},
        "polymarket_us": {"market_id": slug, "ticker": slug, "title": pm_market.get("title"),
                          "yes_bid": p_bid, "yes_ask": p_ask, "quote_observed_at": p_received,
                          "source_timestamp": None,
                          "source_timestamp_semantics": "Official Polymarket US BBO response schema provides no quote-origin timestamp; only NOEMA receipt time is available.",
                          "fees": {**pm_fees,
                                   "source": "Official Polymarket US market response; feeCoefficient retained as raw evidence but its official taker formula, rounding, and applicability are unverified.",
                                   "raw_fee_coefficient": pm_market.get("feeCoefficient")},
                          "raw_depth": p_depth_raw, "normalized_depth": pm_depth,
                          "capacity": "unknown", "book_source": "Polymarket US public market book" if pm_depth else None,
                          "resolution_rules": pm_rules or None, "rule_source_url": pm_url,
                          "market_status_at_observation": {"active": pm_market.get("active"),
                                                            "closed": pm_market.get("closed")}},
        "observed_at": min(k_received, p_received),
        "unadjusted_yes_ask_difference": None if p_ask is None or k_ask is None else round(p_ask-k_ask, 6),
    }


def _count_records(payload: Any, key: str) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        records = payload.get(key)
        if isinstance(records, list):
            return len(records)
        if isinstance(records, dict):
            return len(records)
        if key == "activities":
            for candidate in ("activity", "items", "data"):
                if isinstance(payload.get(candidate), list):
                    return len(payload[candidate])
    return None


async def build_prediction_venue_status(*, force: bool = False) -> dict[str, Any]:
    """Live read-only checks, cached briefly so Home cannot hammer venues."""
    cycle_started_at = datetime.now(UTC).isoformat()
    cycle_started = monotonic()
    now = monotonic()
    if not force and _cache["payload"] is not None and now - _cache["at"] < 15:
        return _cache["payload"]
    async with _lock:
        db_path = os.getenv("NOEMA_DB_PATH", "data/noema.db")
        console_db_path = console_state_db_path(db_path)
        now = monotonic()
        if not force and _cache["payload"] is not None and now - _cache["at"] < 15:
            return _cache["payload"]
        previous_venues = {
            str(item.get("venue")): copy.deepcopy(item)
            for item in ((_cache.get("payload") or {}).get("venues") or [])
            if isinstance(item, dict)
        }
        kalshi, polymarket, comparison = await asyncio.gather(
            _kalshi_status(), _polymarket_us_status(), _cross_venue_candidate(),
        )
        for venue in (kalshi, polymarket):
            _retain_last_successful_account(venue, previous_venues.get(str(venue.get("venue"))))
            _stamp_metric_metadata(venue)
        history_records = []
        for venue in (kalshi, polymarket):
            market = venue.get("market_data") or {}
            account = venue.get("account") or {}
            account_records = account.pop("_persisted_records", None)
            if account_records is not None:
                coverage = account.get("history_coverage", {})
                history_records.append({
                    "venue": str(venue.get("venue", "")).lower().replace(" ", "_"),
                    **account_records,
                    "coverage": {
                        "positions_complete": coverage.get("positions", {}).get("complete") is True,
                        "settlements_complete": coverage.get("settlements", {}).get("complete") is True,
                        "deposits_complete": coverage.get("deposits", {}).get("complete") is True,
                        "withdrawals_complete": coverage.get("withdrawals", {}).get("complete") is True,
                        "open_orders_complete": coverage.get("orders", {}).get("open_snapshot") is True,
                    },
                })
            cash_values = [account.get("cash_balance_usd"), account.get("buying_power_usd")]
            cash_values.extend(
                item.get("current_balance") for item in account.get("cash_balances", [])
                if isinstance(item, dict)
            )
            observed_cash = [_number(value) for value in cash_values if value is not None]
            funded = (any(value > 0 for value in observed_cash) if observed_cash else
                      (False if account.get("balance_available") else None))
            venue["capabilities"] = {
                "connected": market.get("status") == "connected",
                "authenticated": account.get("status") == "authenticated_read_only",
                "readable": (market.get("status") == "connected"
                             or account.get("status") == "authenticated_read_only"),
                "funded": funded,
                "signer_configured": False,
                "credentials_isolated": False,
                "research_enabled": market.get("status") == "connected",
                "paper_enabled": False,
                "mission_authority_present": False,
                "live_execution_enabled": False,
                "halted": True,
                "coordinator_wired": False,
            }
        if history_records:
            persisted_venues = {entry["venue"] for entry in history_records}
            try:
                persistence = await asyncio.to_thread(
                    persist_prediction_account_records,
                    console_db_path,
                    history_records,
                )
                for venue in (kalshi, polymarket):
                    if str(venue.get("venue", "")).lower().replace(" ", "_") in persisted_venues:
                        venue.get("account", {})["history_persistence"] = persistence
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                for venue in (kalshi, polymarket):
                    if str(venue.get("venue", "")).lower().replace(" ", "_") in persisted_venues:
                        venue.get("account", {})["history_persistence"] = {
                            "status": "unavailable", "reason": f"Local source history could not be committed ({type(exc).__name__}).",
                        }
        if comparison.get("matches"):
            comparison["persistence_status"] = await asyncio.to_thread(
                _persist_canonical_observations, comparison["matches"], console_db_path,
            )
            fixed_pairs = await asyncio.to_thread(
                _fixed_lifecycle_pairs, console_db_path, fallback_paths=(db_path,),
            )
            lifecycle_rows = []
            for candidate in comparison.get("matches", [])[:5]:
                contracts = (candidate.get("canonical_identity") or {}).get("contracts", [])
                by_venue = {str(item.get("venue")): item for item in contracts if isinstance(item, dict)}
                pair = (
                    str((by_venue.get("kalshi") or {}).get("contract_id") or ""),
                    str((by_venue.get("polymarket-us") or {}).get("contract_id") or ""),
                )
                if pair not in fixed_pairs:
                    continue
                try:
                    lifecycle_candidate = await _lifecycle_evidence(candidate)
                    if lifecycle_candidate is None:
                        continue
                    evaluation = evaluate_candidate(lifecycle_candidate)
                    lifecycle = await asyncio.to_thread(
                        persist_evaluation, console_db_path, lifecycle_candidate, evaluation,
                    )
                    candidate.update(lifecycle_candidate)
                    candidate["experiment_evaluation"] = evaluation
                    candidate["trial_id"] = lifecycle.get("trial_id")
                    candidate["research_run_id"] = lifecycle.get("run_id")
                    candidate["evidence_hash"] = lifecycle.get("evidence_hash")
                    lifecycle_rows.append(lifecycle["persistence_status"])
                except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
                    candidate["experiment_lifecycle_status"] = "unavailable"
            if comparison["persistence_status"] in {"recorded", "already_recorded"}:
                global _registered_quote_pair, _registered_identity_review_at
                _registered_quote_pair = list(comparison["matches"])
                _registered_identity_review_at = monotonic()
        else:
            comparison["persistence_status"] = "no_candidate_to_record"
        try:
            paper_maturation = await asyncio.to_thread(
                mature_paper_pairs, console_db_path, outcome_path=db_path,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            paper_maturation = {"status": "unavailable", "matured": 0,
                                "walk_forward": None}
        live_resolver_keys: set[tuple[str, str]] = set()
        live_proposition_families: set[str] = set()
        for candidate in comparison.get("matches", []):
            contracts = candidate.get("canonical_identity", {}).get("contracts", [])
            if not contracts:
                continue
            source_identity = contracts[0]
            if source_identity.get("topic_id") == "sports:baseball":
                live_resolver_keys.add(("sports:baseball", "world_series_champion"))
                live_proposition_families.add("winner")
            elif source_identity.get("topic_id") == "company:automotive":
                live_resolver_keys.add(("company:automotive", "q3_deliveries_numeric_threshold"))
                live_proposition_families.add("numeric_threshold")
        live_resolvers = [
            f"{topic}/{family}" for topic, family in sorted(live_resolver_keys)
        ]
        family_coverage = {
            "winner": ("verified_live_overlap" if "winner" in live_proposition_families else "no_live_overlap_verified",
                       "MLB World Series champion is the only registered winner-family discovery probe."),
            "numeric_threshold": ("verified_live_overlap" if "numeric_threshold" in live_proposition_families else "no_live_overlap_verified",
                                  "Tesla Q3 total deliveries use strict Kalshi ticker, Polymarket slug, and rule-text mapping."),
            "occurrence_by_deadline": ("source_resolver_only", "Kalshi NYC daily precipitation is structurally identified; no exact Polymarket US counterpart is registered."),
            "asset_price_at_time": ("source_resolver_only", "Kalshi ETH timestamped price contract is structurally identified; no exact Polymarket US counterpart is registered."),
            "numeric_bucket": ("unresolved_ambiguity", "Bucket boundary inclusivity is not normalized; range rules using 'between' remain unresolved."),
            "economic_release_bucket": ("unresolved_unsupported", "No current source mapping proves a matching release, vintage, bucket boundaries, and resolution source."),
        }
        payload = {
            "as_of": datetime.now(UTC).isoformat(),
            "collection": {
                "cycle_started_at": cycle_started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((monotonic() - cycle_started) * 1000),
                "poll_sleep_after_completion_seconds": 15,
            },
            "execution_enabled": False,
            "canonical_market_identity": {
                "ontology_levels": [
                    "topic", "event", "proposition", "outcome", "semantic_match",
                    "settlement_equivalence", "economic_comparability",
                    "executable_comparability",
                ],
                "registered_resolvers": [
                    {"topic_id": topic, "proposition_family": family}
                    for topic, family in registered_resolvers()
                ],
                "proposition_families": [
                    {
                        "family": family,
                        "resolver_status": "ontology_supported",
                        "production_adapter_status": family_coverage[family][0],
                        "coverage_reason": family_coverage[family][1],
                    }
                    for family in PROPOSITION_FAMILIES
                ],
                "coverage": {
                    "live_resolver_families": live_resolvers,
                    "verified_live_overlap_count": len(comparison.get("matches", [])),
                    "discovery_scope": "registered live overlap probes only; not a full-catalog scan",
                    "unresolved_families": [
                        {"family": family, "status": status, "reason": reason}
                        for family, (status, reason) in family_coverage.items()
                        if status in {"source_resolver_only", "unresolved_ambiguity", "unresolved_unsupported"}
                    ],
                    "unresolved_candidates": [] if comparison.get("matches") else [{
                        "status": comparison.get("status", "unresolved"),
                        "reason": comparison.get("reason", "No evidence-backed overlap is available."),
                    }],
                    "rejection_reasons": [] if comparison.get("matches") else [
                        comparison.get("reason", "No evidence-backed overlap is available.")
                    ],
                    "unresolved_reason": comparison.get("reason"),
                    "slow_path": "discovery and canonical identity assessment",
                    "fast_path": "registered identity links only; quote refresh must not rerun resolver",
                },
                "unsupported_families": "remain unresolved; no fuzzy identity promotion",
                "settlement_equivalence_default": "unverified",
                "execution_authority": "disabled",
            },
            "venues": [kalshi, polymarket],
            "cross_venue_comparison": comparison,
            "cross_venue_paper_maturation": paper_maturation,
            "cross_venue_experiment_history": await asyncio.to_thread(
                recent_evaluations, console_db_path, limit=20,
            ),
        }
        _cache.update(at=monotonic(), payload=payload)
        return payload


def _persist_canonical_observations(
    observations: list[dict[str, Any]], path: str | None = None,
) -> str:
    ledger: ForecastLedger | None = None
    economic_ledger = None
    database_path = path or console_state_db_path()
    try:
        ledger = ForecastLedger(database_path)
        inserted_observations = [
            item for item in observations if ledger.append_canonical_pair_observation(item)
        ]
        inserted = len(inserted_observations)
        if inserted_observations:
            # Canonical observations enter the existing economy event stream as
            # non-monetary provenance. No parallel accounting store is created.
            from .economic_ledger import EconomicLedger

            economic_ledger = EconomicLedger(database_path)
            for item in inserted_observations:
                append_shared_economic_event(
                    economic_ledger, canonical_observation_event(item), amount_usd=None,
                )
        return "recorded" if inserted else "already_recorded"
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "unavailable"
    finally:
        if ledger is not None:
            ledger.conn.close()
        if economic_ledger is not None:
            economic_ledger.conn.close()
