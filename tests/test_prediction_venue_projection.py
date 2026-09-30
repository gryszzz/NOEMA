from noema.prediction_venues import (
    _kalshi_account_metrics,
    _polymarket_fills,
    _polymarket_orders,
    _polymarket_position_capital,
    _polymarket_positions,
)


def test_polymarket_positions_are_bounded_and_usd_values_are_currency_checked():
    payload = {"positions": {
        "position-1": {
            "netPositionDecimal": "2.5", "qtyAvailableDecimal": "2",
            "realized": {"value": "0.25", "currency": "USD"},
            "cashValue": {"value": "1.10", "currency": "USD"},
            "cost": {"value": "0.90", "currency": "USD"},
            "fees": {"value": "0.02", "currency": "USD"},
            "updateTime": "2026-09-29T12:00:00Z",
            "marketMetadata": {"slug": "btc-15m", "title": "BTC 15m", "outcome": "Yes"},
            "private_field": "must not leave the adapter",
        },
    }, "nextCursor": None, "eof": True}

    rows = _polymarket_positions(payload)
    assert rows == [{
        "ticker": "btc-15m", "title": "BTC 15m", "outcome": "Yes",
        "position_fp": "2.5", "available_fp": "2",
        "realized_pnl_dollars": "0.25", "market_value_usd": "1.1",
        "cost_basis_usd": "0.9", "fees_paid_dollars": "0.02",
        "last_updated_ts": "2026-09-29T12:00:00Z",
    }]
    capital = _polymarket_position_capital(rows, payload, [])
    # Position realized is a current-position subtotal, not account-wide
    # historical realized P&L when activity pagination has not been proven.
    assert capital["reported_realized_pnl_usd"] is None
    assert rows[0]["realized_pnl_dollars"] == "0.25"
    assert capital["reported_exposure_usd"] == "1.1"
    assert capital["more_positions"] is False


def test_polymarket_fills_and_orders_use_shared_safe_terminal_fields():
    fills = _polymarket_fills({"activities": [{"type": "ACTIVITY_TYPE_TRADE", "trade": {
        "id": "trade-1", "marketSlug": "btc-15m", "qty": "2", "price": {
            "value": "0.40", "currency": "USD"}, "costBasis": {"value": "0.80", "currency": "USD"},
        "realizedPnl": {"value": "0.10", "currency": "USD"},
        "createTime": "2026-09-29T12:00:00Z", "private_field": "dropped",
    }}]})
    assert fills == [{
        "fill_id": "trade-1", "ticker": "btc-15m", "count_fp": "2", "side": None,
        "fee_cost": None, "price_usd": "0.4", "cost_basis_usd": "0.8",
        "realized_pnl_usd": "0.1", "created_time": "2026-09-29T12:00:00Z", "state": None,
        "title": None,
    }]
    orders = _polymarket_orders({"orders": [{
        "id": "order-1", "marketSlug": "btc-15m", "status": "OPEN", "side": "BUY",
        "cumQuantity": "1", "leavesQuantity": "2", "createTime": "now", "credential": "dropped",
    }]})
    assert orders == [{
        "order_id": "order-1", "ticker": "btc-15m", "status": "OPEN", "side": "BUY",
        "fill_count_fp": "1", "remaining_count_fp": "2",
        "created_time": "now", "last_update_time": None,
    }]


def test_polymarket_cursor_pages_deduplicate_overlapping_activity_and_gate_totals():
    import asyncio

    from noema.prediction_venues import _polymarket_cursor_pages

    calls = []
    first = {"type": "ACTIVITY_TYPE_TRADE", "trade": {
        "id": "fill-a", "qty": "2", "price": {"value": "0.4", "currency": "USD"},
        "realizedPnl": {"value": "0.1", "currency": "USD"},
    }}
    second = {"type": "ACTIVITY_TYPE_TRADE", "trade": {
        "id": "fill-b", "qty": "3", "price": {"value": "0.5", "currency": "USD"},
        "realizedPnl": {"value": "0.2", "currency": "USD"},
    }}

    def page(params):
        calls.append(params)
        if params.get("cursor") is None:
            return {"activities": [first], "nextCursor": "page-2", "eof": False}
        return {"activities": [first, second], "nextCursor": "", "eof": True}

    payload = asyncio.run(_polymarket_cursor_pages(page, "activities"))
    fills = _polymarket_fills(payload)
    capital = _polymarket_position_capital([], {"positions": {}, "eof": True}, fills,
                                           activity_complete=payload["pagination"]["complete"])
    assert [row["fill_id"] for row in fills] == ["fill-a", "fill-b"]
    assert capital["observed_volume_usd"] == "2.3"
    assert capital["reported_realized_pnl_usd"] == "0.3"
    assert payload["pagination"] == {
        "complete": True, "pages": 2, "records": 2, "reason": None,
        "incremental": False, "reached_anchor": False, "delta_complete": True,
    }
    assert calls == [
        {"limit": 100, "sortOrder": "SORT_ORDER_DESCENDING"},
        {"limit": 100, "sortOrder": "SORT_ORDER_DESCENDING", "cursor": "page-2"},
    ]


def test_polymarket_cursor_pagination_stays_incomplete_on_repeated_cursor():
    import asyncio

    from noema.prediction_venues import _polymarket_cursor_pages

    def page(params):
        return {"activities": [], "nextCursor": "loop", "eof": False}

    payload = asyncio.run(_polymarket_cursor_pages(page, "activities"))
    assert payload["pagination"]["complete"] is False
    assert payload["pagination"]["reason"] == "repeated_cursor"


def test_polymarket_incremental_activity_stops_after_persisted_anchor():
    import asyncio

    from noema.prediction_venues import _polymarket_cursor_pages

    recent = {"type": "ACTIVITY_TYPE_TRADE", "trade": {"id": "trade-new"}}
    anchor = {"type": "ACTIVITY_TYPE_TRADE", "trade": {"id": "trade-known"}}
    calls = []

    def page(params):
        calls.append(params)
        return {"activities": [recent, anchor], "nextCursor": "old-page", "eof": False}

    payload = asyncio.run(_polymarket_cursor_pages(
        page, "activities", stop_after_ids={"ACTIVITY_TYPE_TRADE:trade-known"},
    ))
    assert payload["pagination"]["complete"] is True
    assert payload["pagination"]["delta_complete"] is True
    assert payload["pagination"]["reached_anchor"] is True
    assert payload["pagination"]["incremental"] is True
    assert payload["activities"] == [recent, anchor]
    assert len(calls) == 1


def test_polymarket_deposits_withdrawals_and_transfers_are_persistable_activity():
    from noema.prediction_venues import _polymarket_balance_activities

    payload = {"activities": [
        {"type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "accountBalanceChange": {"transactions": [
            {"transactionId": "deposit-1", "amount": {"value": "5", "currency": "USD"}, "status": "COMPLETE"},
        ]}},
        {"type": "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL", "accountBalanceChange": {"transactions": [
            {"transactionId": "withdrawal-1", "amount": {"value": "2", "currency": "USD"}, "status": "COMPLETE"},
        ]}},
        {"type": "ACTIVITY_TYPE_TRANSFER", "accountBalanceChange": {"transactions": [
            {"transactionId": "transfer-1", "amount": {"value": "1", "currency": "USD"}, "status": "COMPLETE"},
        ]}},
    ]}
    rows = _polymarket_balance_activities(payload)
    assert [(row["activity_type"], row["transaction_id"], row["amount_usd"]) for row in rows] == [
        ("ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "deposit-1", "5"),
        ("ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL", "withdrawal-1", "2"),
        ("ACTIVITY_TYPE_TRANSFER", "transfer-1", "1"),
    ]


def test_kalshi_authenticated_cashflow_requires_balance_reconciliation_for_realized_pnl():
    complete = {"complete": True, "pages": 1}
    report = _kalshi_account_metrics(
        {"balance_dollars": "10", "portfolio_value_dollars": "10.30"},
        {"market_positions": [{"ticker": "MKT-A", "market_exposure_dollars": "0.30"}],
         "pagination": complete},
        {"fills": [{"fill_id": "fill-a", "outcome_side": "yes", "book_side": "bid",
                    "count_fp": "2", "yes_price_dollars": "0.40", "fee_cost": "0.01"}],
         "pagination": complete},
        {"settlements": [{"ticker": "MKT-A", "settled_time": "2026-09-29T12:00:00Z",
                          "revenue": "90", "fee_cost": "0.02"}],
         "pagination": complete},
        {"deposits": [], "pagination": complete},
        {"withdrawals": [], "pagination": complete},
    )
    assert report["volume"]["value"] == "0.80"
    assert report["fees"]["value"] == "0.03"
    assert report["realized_pnl"]["value"] is None
    reconciliation = report["realized_pnl"]["components"]["cashflow_reconciliation"]
    assert reconciliation is None
    assert "opening-balance history" in report["realized_pnl"]["reason"]
    assert report["exposure"]["value"] == "0.30"


def test_live_fill_attribution_requires_exact_gateway_order_reference(tmp_path):
    import json
    import sqlite3

    from noema.prediction_venues import _execution_context_by_order

    path = tmp_path / "gateway.db"
    with sqlite3.connect(path) as conn:
        conn.execute("create table execution_gateway_requests (route text, provider_reference text, request_json text, created_at text)")
        conn.execute("insert into execution_gateway_requests values (?,?,?,?)", (
            "prediction", "venue-order-1",
            json.dumps({"decision_id": "decision-1", "strategy_id": "strategy-1", "experiment_id": None}),
            "2026-09-29T12:00:00Z",
        ))
        conn.commit()
    contexts = _execution_context_by_order(str(path))
    assert contexts == {"venue-order-1": {"decision_id": "decision-1", "strategy_id": "strategy-1"}}
    assert "unknown-order" not in contexts
