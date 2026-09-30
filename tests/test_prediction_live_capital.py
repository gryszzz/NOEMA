from __future__ import annotations

import asyncio

from noema.account import KalshiAccount
from noema.prediction_account_history import get_or_create_prediction_account_baseline
from noema.prediction_venues import (
    _kalshi_account_metrics,
    _kalshi_open_position_count,
    _polymarket_position_capital,
    _retain_last_successful_account,
)


def test_kalshi_realized_pnl_requires_complete_reconciled_opening_balance_window():
    complete = {"complete": True, "pages": 1}
    baseline = {"observed_at": "2026-09-29T11:00:00+00:00", "cash_usd": "20", "portfolio_value_usd": "0"}
    metrics = _kalshi_account_metrics(
        {"balance_dollars": "21.17", "portfolio_value_dollars": "0"},
        {"market_positions": [{"ticker": "MKT-CLOSED", "position_fp": "0",
                                "market_exposure_dollars": "0"}], "pagination": complete},
        {"fills": [{"fill_id": "fill-1", "outcome_side": "yes", "book_side": "bid",
                    "count_fp": "2", "yes_price_dollars": "0.40", "fee_cost": "0.01",
                    "created_time": "2026-09-29T11:30:00Z"}],
         "pagination": complete},
        {"settlements": [{"ticker": "MKT-1", "settled_time": "2026-09-29T12:00:00Z",
                           "revenue": 200, "fee_cost": "0.02"}], "pagination": complete},
        {"deposits": [], "pagination": complete},
        {"withdrawals": [], "pagination": complete},
        {"transfers": [], "pagination": complete},
        baseline,
    )

    assert metrics["balance"]["value"] == "21.17"
    assert metrics["account_value"]["value"] == "0"
    assert metrics["realized_pnl"]["value"] == "1.17"
    assert metrics["realized_pnl"]["provenance"] == "DERIVED_FROM_LIVE_RECORDS"
    assert metrics["realized_pnl"]["components"]["cashflow_reconciliation"]["state"] == "RECONCILED"
    assert metrics["exposure"]["value"] == "0"
    assert metrics["volume"]["value"] == "0.80"
    assert metrics["fees"]["value"] == "0.03"
    assert metrics["unrealized_pnl"]["value"] is None


def test_kalshi_flat_baseline_ignores_historical_zero_position_rows():
    historical_rows = [
        {"ticker": "MKT-CLOSED", "position_fp": "0"},
        {"ticker": "MKT-OPEN", "position_fp": "2.5"},
    ]
    assert _kalshi_open_position_count(historical_rows) == 1
    flat_history = [{"ticker": "MKT-CLOSED", "position_fp": "0"}]
    assert _kalshi_open_position_count(flat_history) == 0
    assert _kalshi_open_position_count([{"ticker": "MKT-UNKNOWN"}]) is None


def test_kalshi_flat_baseline_persists_with_only_historical_zero_position_rows(tmp_path):
    open_positions = _kalshi_open_position_count([
        {"ticker": "MKT-CLOSED", "position_fp": "0"},
    ])
    baseline = get_or_create_prediction_account_baseline(
        tmp_path / "baseline.db", "kalshi", observed_at="2026-09-30T12:00:00+00:00",
        cash_usd="25", portfolio_value_usd="0", positions_complete=True,
        open_positions=open_positions,
    )
    assert baseline["cash_usd"] == "25"


def test_kalshi_realized_pnl_stays_unavailable_without_opening_baseline():
    complete = {"complete": True}
    report = _kalshi_account_metrics(
        {"balance_dollars": "20", "portfolio_value_dollars": "0"},
        {"market_positions": [], "pagination": complete},
        {"fills": [], "pagination": complete},
        {"settlements": [], "pagination": complete},
        {"deposits": [], "pagination": complete},
        {"withdrawals": [], "pagination": complete},
        {"transfers": [], "pagination": complete},
    )
    assert report["realized_pnl"]["value"] is None
    assert "opening-balance history" in report["realized_pnl"]["reason"]


def test_polymarket_zero_positions_are_zero_and_missing_realized_has_exact_reason():
    fills = [
        {"fill_id": "trade-1", "count_fp": "2", "price_usd": "0.40", "realized_pnl_usd": "0.10"},
        {"fill_id": "trade-2", "count_fp": "3", "price_usd": "0.20", "realized_pnl_usd": None},
    ]
    capital = _polymarket_position_capital(
        [], {"positions": {}, "pagination": {"complete": True}, "eof": True}, fills,
        activity_complete=True, resolution_rows=[],
    )

    assert capital["metrics"]["exposure"]["value"] == "0"
    assert capital["metrics"]["unrealized_pnl"]["value"] == "0"
    assert capital["metrics"]["volume"]["value"] == "1.40"
    assert capital["metrics"]["realized_pnl"]["value"] is None
    assert capital["metrics"]["realized_pnl"]["reason"] == (
        "1 of 2 trade activities omit realizedPnl; 0 are linked to position-resolution records, "
        "which is insufficient to reconcile every trade."
    )
    assert "no per-trade fee amount" in capital["metrics"]["fees"]["reason"]


def test_polymarket_position_resolution_delta_fills_missing_trade_realized_pnl():
    fills = [{"fill_id": "trade-1", "count_fp": "1", "price_usd": "0.5", "realized_pnl_usd": None}]
    capital = _polymarket_position_capital(
        [], {"positions": {}, "pagination": {"complete": True}, "eof": True}, fills,
        activity_complete=True,
        resolution_rows=[{"trade_id": "trade-1", "before_position": {"realized": {"value": "0.1", "currency": "USD"}},
                          "after_position": {"realized": {"value": "0.4", "currency": "USD"}}}],
    )
    assert capital["metrics"]["realized_pnl"]["value"] == "0.3"


def test_kalshi_cursor_pagination_reads_each_page_and_combines_collections():
    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Client:
        def __init__(self):
            self.calls = []

        async def get(self, endpoint, *, params, headers):
            self.calls.append((endpoint, params, headers))
            if params.get("cursor") == "next":
                return Response({"market_positions": [{"ticker": "M2"}], "event_positions": [], "cursor": ""})
            return Response({"market_positions": [{"ticker": "M1"}], "event_positions": [{"event_ticker": "E1"}], "cursor": "next"})

    class Signer:
        def headers(self, method, path):
            return {"test": "signed"}

    account = object.__new__(KalshiAccount)
    account.client, account.signer = Client(), Signer()
    result = asyncio.run(account._get_all_pages(
        "/portfolio/positions", "market_positions", params={"subaccount": 0},
        additional_collections=("event_positions",), limit=1000,
    ))
    assert [row["ticker"] for row in result["market_positions"]] == ["M1", "M2"]
    assert result["event_positions"] == [{"event_ticker": "E1"}]
    assert result["pagination"] == {"complete": True, "pages": 2,
                                     "records": {"market_positions": 2, "event_positions": 1}}
    assert len(account.client.calls) == 2


def test_kalshi_incremental_activity_uses_documented_min_timestamp_filter():
    account = object.__new__(KalshiAccount)
    calls = []

    async def pages(endpoint, collection, *, params, limit, additional_collections=()):
        calls.append((endpoint, params, limit))
        return {collection: [], "pagination": {"complete": True}}

    account._get_all_pages = pages
    asyncio.run(account.fills(min_ts=1_790_000_000))
    asyncio.run(account.settlements(min_ts=1_790_000_000))
    assert calls == [
        ("/portfolio/fills", {"subaccount": 0, "min_ts": 1_790_000_000}, 1000),
        ("/portfolio/settlements", {"subaccount": 0, "min_ts": 1_790_000_000}, 1000),
    ]


def test_venue_failure_retains_last_success_as_explicitly_stale_without_reobserving():
    previous = {"account": {
        "status": "authenticated_read_only", "observed_at": "2026-09-29T12:00:00Z",
        "_persisted_records": {"fills": [{"fill_id": "secret-record"}]},
        "account_metrics": {"balance": {"value": "10", "freshness": "LIVE"}},
    }}
    current = {"venue": "Kalshi", "account": {
        "status": "authentication_failed", "error_type": "timeout",
    }}
    _retain_last_successful_account(current, previous)
    account = current["account"]
    assert account["status"] == "stale_authenticated_read_only"
    assert account["observed_at"] == "2026-09-29T12:00:00Z"
    assert account["account_metrics"]["balance"]["freshness"] == "STALE"
    assert account["account_metrics"]["balance"]["reconciliation_state"] == "STALE_OBSERVATION"
    assert "_persisted_records" not in account


def test_kalshi_repeated_page_cursor_fails_closed():
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"fills": [], "cursor": "same"}

    class Client:
        async def get(self, *args, **kwargs):
            return Response()

    class Signer:
        def headers(self, method, path):
            return {}

    account = object.__new__(KalshiAccount)
    account.client, account.signer = Client(), Signer()
    try:
        asyncio.run(account._get_all_pages("/portfolio/fills", "fills", params={}, limit=1000))
    except ValueError as exc:
        assert "repeated pagination cursor" in str(exc)
    else:
        raise AssertionError("repeated cursor must not be reported as a complete scan")
