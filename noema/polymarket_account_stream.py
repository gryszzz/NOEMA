"""Read-only Polymarket US private account stream into existing history storage."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .prediction_account_history import persist_prediction_account_records
from .wallet_credentials import (
    load_polymarket_us_credentials_in_api_boundary,
    polymarket_us_credentials_present,
)

_log = logging.getLogger(__name__)
_stream_health: dict[str, Any] = {
    "state": "not_started", "connected_at": None, "last_message_at": None,
    "last_persisted_at": None, "events_received": 0, "reconnects": 0,
    "last_error_type": None,
}


def polymarket_stream_health() -> dict[str, Any]:
    return copy.deepcopy(_stream_health)


def _observed_at(payload: dict[str, Any]) -> str:
    for key in ("updateTime", "updatedAt", "timestamp", "transactTime"):
        value = payload.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is not None:
                    return parsed.astimezone(UTC).isoformat()
            except ValueError:
                pass
    return datetime.now(UTC).isoformat()


def _account_rows(message: dict[str, Any]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
]:
    """Extract documented after-state records; malformed events stay unpersisted."""
    balances: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    balance_snapshot = message.get("accountBalancesSnapshot")
    snapshot_rows = balance_snapshot.get("balances") if isinstance(balance_snapshot, dict) else None
    if isinstance(snapshot_rows, list):
        balances.extend(row for row in snapshot_rows if isinstance(row, dict))
    balance_update = message.get("accountBalancesUpdate")
    if isinstance(balance_update, dict):
        change = balance_update.get("balanceChange")
        after = change.get("afterBalance") if isinstance(change, dict) else None
        if isinstance(after, dict):
            balances.append(after)
    # Compatibility with the SDK's legacy event envelope.
    legacy_snapshot = message.get("accountBalanceSubscriptionSnapshot")
    if isinstance(legacy_snapshot, dict):
        rows = legacy_snapshot.get("balances")
        if isinstance(rows, list):
            balances.extend(row for row in rows if isinstance(row, dict))
    legacy_update = message.get("accountBalanceSubscriptionUpdate") or message.get("accountBalanceUpdate")
    if isinstance(legacy_update, dict):
        change = legacy_update.get("balanceChange") or legacy_update
        after = change.get("afterBalance") if isinstance(change, dict) else None
        if isinstance(after, dict):
            balances.append(after)

    position = message.get("positionSubscription")
    if isinstance(position, dict) and isinstance(position.get("afterPosition"), dict):
        positions.append(position["afterPosition"])
    legacy_position = message.get("positionSubscriptionUpdate") or message.get("positionUpdate")
    if isinstance(legacy_position, dict):
        after = legacy_position.get("afterPosition") or legacy_position.get("position")
        if isinstance(after, dict):
            positions.append(after)
    order_snapshot = message.get("orderSubscriptionSnapshot") or message.get("ordersSnapshot")
    snapshot_rows = order_snapshot.get("orders") if isinstance(order_snapshot, dict) else None
    if isinstance(snapshot_rows, list):
        for row in snapshot_rows:
            if isinstance(row, dict) and (row.get("id") or row.get("order_id")):
                orders.append({
                    "order_id": str(row.get("id") or row.get("order_id")),
                    "ticker": row.get("marketSlug") or row.get("ticker"),
                    "status": row.get("state") or row.get("status"),
                    "side": row.get("side"),
                    "fill_count_fp": row.get("cumQuantity") or row.get("fill_count_fp"),
                    "remaining_count_fp": row.get("leavesQuantity") or row.get("remaining_count_fp"),
                    "created_time": row.get("createTime") or row.get("created_time"),
                    "last_update_time": row.get("updateTime") or row.get("last_update_time"),
                })
    order_update = message.get("orderSubscriptionUpdate") or message.get("orderUpdate")
    execution = order_update.get("execution") if isinstance(order_update, dict) else None
    if isinstance(execution, dict):
        order = execution.get("order") if isinstance(execution.get("order"), dict) else {}
        order_id = order.get("id") or execution.get("orderId")
        if order_id:
            orders.append({
                "order_id": str(order_id),
                "ticker": order.get("marketSlug"),
                "status": order.get("state"),
                "side": order.get("side"),
                "fill_count_fp": order.get("cumQuantity"),
                "remaining_count_fp": order.get("leavesQuantity"),
                "last_update_time": execution.get("transactTime"),
            })
        trade_id = execution.get("tradeId")
        price = execution.get("lastPx")
        fee = execution.get("commissionNotionalCollected")
        quantity = execution.get("lastShares")
        price_usd = (price.get("value") if isinstance(price, dict)
                     and str(price.get("currency", "")).upper() == "USD" else None)
        if trade_id and price_usd is not None and quantity is not None:
            fills.append({
                "fill_id": str(trade_id), "order_id": str(order_id) if order_id else None,
                "ticker": order.get("marketSlug"), "count_fp": str(quantity),
                "price_usd": price_usd,
                "side": order.get("side"), "fee_cost": (
                    fee.get("value") if isinstance(fee, dict)
                    and str(fee.get("currency", "")).upper() == "USD" else None
                ),
                "realized_pnl_usd": None, "created_time": execution.get("transactTime"),
                "state": execution.get("type"),
            })
    return balances, positions, orders, fills


async def run_polymarket_account_stream(
    db_path: Callable[[], str], on_change: Callable[[dict[str, Any]], None],
) -> None:
    """Reconnect indefinitely; REST collection remains the startup/recovery snapshot."""
    delay = 1.0
    while True:
        websocket = None
        try:
            key_id_present, secret_present = polymarket_us_credentials_present()
            if not key_id_present or not secret_present:
                _stream_health.update(state="unconfigured")
                await asyncio.sleep(15)
                continue
            from polymarket_us import PolymarketUS

            key_id, secret_key = load_polymarket_us_credentials_in_api_boundary()
            client = PolymarketUS(key_id=key_id, secret_key=secret_key, timeout=5.0, max_retries=0)
            websocket = client.ws.private()

            def mark_message() -> None:
                _stream_health["last_message_at"] = datetime.now(UTC).isoformat()
                _stream_health["events_received"] += 1

            def persist(message: Any) -> None:
                if not isinstance(message, dict):
                    return
                mark_message()
                balances, raw_positions, orders, fills = _account_rows(message)
                if not balances and not raw_positions and not orders and not fills:
                    return
                observed = datetime.now(UTC).isoformat()
                normalized_positions = []
                for row in raw_positions:
                    market = row.get("marketMetadata") if isinstance(row.get("marketMetadata"), dict) else {}
                    slug = row.get("marketSlug") or market.get("slug")
                    if not slug:
                        continue
                    normalized_positions.append({
                        "ticker": slug,
                        "outcome": row.get("outcome") or market.get("outcome"),
                        "position_fp": row.get("netPositionDecimal", row.get("netPosition", "0")),
                        "available_fp": row.get("qtyAvailableDecimal", row.get("qtyAvailable")),
                        "market_value_usd": (row.get("cashValue") or {}).get("value")
                        if isinstance(row.get("cashValue"), dict) else row.get("cashValue"),
                        "cost_basis_usd": (row.get("cost") or {}).get("value")
                        if isinstance(row.get("cost"), dict) else row.get("cost"),
                        "realized_pnl_dollars": (row.get("realized") or {}).get("value")
                        if isinstance(row.get("realized"), dict) else row.get("realized"),
                        "fees_paid_dollars": (row.get("fees") or {}).get("value")
                        if isinstance(row.get("fees"), dict) else row.get("fees"),
                        "last_updated_ts": _observed_at(row),
                    })
                normalized_balances = []
                for row in balances:
                    item = dict(row)
                    item["observed_at"] = _observed_at(row) if any(
                        key in row for key in ("updateTime", "updatedAt", "timestamp", "transactTime")
                    ) else observed
                    normalized_balances.append(item)
                records = [{
                    "venue": "polymarket_us",
                    "balance": ({"observed_at": observed, "balances": normalized_balances}
                                if normalized_balances else None),
                    "positions": normalized_positions,
                    "orders": orders,
                    "fills": fills,
                }]
                try:
                    persist_prediction_account_records(db_path(), records, observed_at=observed)
                    _stream_health["last_persisted_at"] = observed
                    on_change({"balances": normalized_balances,
                               "positions": normalized_positions, "orders": orders,
                               "fills": fills})
                except Exception:  # noqa: BLE001 - keep the socket alive; never log payloads
                    _log.warning("Polymarket account event could not be persisted")

            websocket.on("account_balance_snapshot", persist)
            websocket.on("account_balance_update", persist)
            websocket.on("position_update", persist)
            websocket.on("order_update", persist)
            websocket.on("order_snapshot", persist)
            websocket.on("heartbeat", mark_message)
            websocket.on("open", lambda: _stream_health.update(
                state="connected", connected_at=datetime.now(UTC).isoformat(), last_error_type=None,
            ))
            websocket.on("error", lambda error: (
                _stream_health.update(state="degraded", last_error_type=type(error).__name__),
                _log.warning("Polymarket private account stream reported an error"),
            ))
            websocket.on("close", lambda: (
                _stream_health.update(state="disconnected"),
                _log.warning("Polymarket private account stream closed"),
            ))
            await websocket.connect()
            await websocket.subscribe_account_balance("noema-account-balance")
            await websocket.subscribe_positions("noema-account-positions")
            await websocket.subscribe_orders("noema-account-orders")
            await websocket.subscribe("noema-account-order-snapshot", "SUBSCRIPTION_TYPE_ORDER_SNAPSHOT")
            delay = 1.0
            while websocket.is_connected:
                await asyncio.sleep(1)
            raise RuntimeError("private account stream disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - websocket SDK errors are provider-specific
            _stream_health.update(state="degraded", last_error_type=type(exc).__name__)
            _log.warning("Polymarket private account stream reconnecting (%s)", type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)
        finally:
            if websocket is not None:
                _stream_health["reconnects"] += 1
                try:
                    await websocket.close()
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask the reconnect loop
                    _log.debug("Polymarket private stream cleanup failed (%s)", type(exc).__name__)
