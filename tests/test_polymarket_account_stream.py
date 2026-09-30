import sqlite3

from noema import prediction_venues
from noema.polymarket_account_stream import _account_rows
from noema.prediction_account_history import persist_prediction_account_records


def test_private_balance_position_and_order_updates_normalize_to_existing_records(tmp_path):
    message = {
        "accountBalancesUpdate": {"balanceChange": {"afterBalance": {
            "currency": "USD", "currentBalance": {"value": "12.50", "currency": "USD"},
            "updateTime": "2026-09-29T12:00:00Z",
        }}},
        "positionSubscription": {"afterPosition": {
            "marketSlug": "market-a", "netPositionDecimal": "2.0",
            "marketMetadata": {"outcome": "YES"},
        }},
        "orderSubscriptionUpdate": {"execution": {
            "orderId": "order-a", "marketSlug": "market-a", "status": "filled",
        }},
    }
    balances, positions, orders, fills = _account_rows(message)
    assert len(balances) == len(positions) == len(orders) == 1
    assert fills == []

    result = persist_prediction_account_records(str(tmp_path / "accounts.db"), [{
        "venue": "polymarket_us", "balance": {"observed_at": "2026-09-29T12:00:00+00:00", "balances": balances},
        "positions": [{
            "ticker": positions[0]["marketSlug"], "outcome": positions[0]["marketMetadata"]["outcome"],
            "position_fp": positions[0]["netPositionDecimal"],
        }],
        "orders": orders,
    }])
    assert result["inserted"] == 3
    replay = persist_prediction_account_records(str(tmp_path / "accounts.db"), [{
        "venue": "polymarket_us", "balance": {"observed_at": "2026-09-29T12:00:00+00:00", "balances": balances},
        "positions": [{
            "ticker": positions[0]["marketSlug"], "outcome": positions[0]["marketMetadata"]["outcome"],
            "position_fp": positions[0]["netPositionDecimal"],
        }],
        "orders": orders,
    }])
    assert replay["inserted"] == 0
    assert replay["updated"] == 0
    assert replay["skipped"] == 3


def test_private_balance_update_keeps_unknown_fields_unknown():
    balances, positions, orders, fills = _account_rows({
        "accountBalancesUpdate": {"balanceChange": {"afterBalance": {
            "currency": "USD", "buyingPower": None,
        }}}
    })
    assert balances == [{"currency": "USD", "buyingPower": None}]
    assert positions == []
    assert orders == []
    assert fills == []


def test_private_fill_requires_usd_price_and_has_stable_trade_id():
    _, _, orders, fills = _account_rows({"orderSubscriptionUpdate": {"execution": {
        "tradeId": "trade-1", "lastShares": "2.5",
        "lastPx": {"value": "0.4", "currency": "USD"},
        "transactTime": "2026-09-29T12:00:00Z",
        "order": {"id": "order-1", "marketSlug": "market-a", "state": "ORDER_STATE_FILLED"},
    }}})
    assert orders[0]["order_id"] == "order-1"
    assert fills == [{
        "fill_id": "trade-1", "order_id": "order-1", "ticker": "market-a",
        "count_fp": "2.5", "price_usd": "0.4", "side": None, "fee_cost": None,
        "realized_pnl_usd": None, "created_time": "2026-09-29T12:00:00Z",
        "state": None,
    }]


def test_legacy_execution_order_fields_fall_back_to_execution_envelope(tmp_path):
    _, _, orders, fills = _account_rows({"orderSubscriptionUpdate": {"execution": {
        "orderId": "legacy-order-1", "marketSlug": "legacy-market", "status": "OPEN",
        "side": "BUY_YES", "cumQuantity": "2", "leavesQuantity": "3",
        "tradeId": "legacy-trade-1", "lastShares": "1",
        "lastPx": {"value": "0.5", "currency": "USD"},
    }}})
    assert orders == [{
        "order_id": "legacy-order-1", "ticker": "legacy-market", "status": "OPEN",
        "side": "BUY_YES", "fill_count_fp": "2", "remaining_count_fp": "3",
        "last_update_time": None,
    }]
    assert fills[0]["ticker"] == "legacy-market"
    assert fills[0]["side"] == "BUY_YES"
    database = tmp_path / "legacy-order.db"
    persist_prediction_account_records(str(database), [{"venue": "polymarket_us", "orders": orders}])
    with sqlite3.connect(database) as conn:
        market_id, state, payload = conn.execute(
            "SELECT market_id,state,payload_json FROM prediction_account_records "
            "WHERE venue='polymarket_us' AND record_type='order'"
        ).fetchone()
    assert (market_id, state) == ("legacy-market", "OPEN")
    assert '"status":"OPEN"' in payload


def test_authenticated_balance_after_state_updates_cached_projection_without_a_rest_read():
    previous = prediction_venues._cache.copy()
    prediction_venues._cache.update(at=0.0, payload={
        "as_of": "2026-09-29T11:00:00+00:00",
        "venues": [{"venue": "Polymarket US", "account": {
            "status": "authenticated_read_only", "cash_balance_usd": "10",
            "observed_at": "2026-09-29T11:00:00+00:00", "fills": 0, "recent_fills": [],
            "history_coverage": {"activities": {"complete": True}},
            "capital": {"metrics": {"volume": {"value": "10", "source_ids": ["old"], "components": {}}}},
        }}],
    })
    try:
        update = {"balances": [{
            "currency": "USD", "currentBalance": {"value": "12.50", "currency": "USD"},
        }], "fills": [{"fill_id": "trade-new", "count_fp": "2.5", "price_usd": "0.4"}]}
        prediction_venues.apply_polymarket_stream_projection(update)
        prediction_venues.apply_polymarket_stream_projection(update)
        result = prediction_venues.cached_prediction_venue_status()
        account = result["venues"][0]["account"]
        assert account["cash_balance_usd"] == "12.5"
        assert account["capital"]["metrics"]["balance"]["source_ids"] == ["private-ws:account-balance"]
        assert account["capital"]["metrics"]["volume"]["value"] == "11.00"
        assert account["fills"] == 1
        assert account["observed_at"] == result["as_of"]
    finally:
        prediction_venues._cache.clear()
        prediction_venues._cache.update(previous)
