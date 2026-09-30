from noema.account_capital import kalshi_cash_usd, kalshi_observed_capital


def test_cash_uses_dollars_or_explicit_cents_and_preserves_unknown():
    assert kalshi_cash_usd({"balance_dollars": "1.2345", "balance": 123}) == "1.2345"
    assert kalshi_cash_usd({"balance": 123}) == "1.23"
    assert kalshi_cash_usd({"balance": 0}) == "0"
    for value in (None, "NaN", "Infinity", True, ""):
        assert kalshi_cash_usd({"balance_dollars": value, "balance": 500}) is None
    assert kalshi_cash_usd({}) is None


def test_observed_volume_has_bounded_scope_and_strict_fields():
    rows = [{"ticker": "A", "total_traded_dollars": "15.30",
             "realized_pnl_dollars": "-2", "fees_paid_dollars": "0.15"},
            {"ticker": "B", "total_traded_dollars": "0",
             "realized_pnl_dollars": "0", "fees_paid_dollars": "0"}]
    result = kalshi_observed_capital({"market_positions": rows, "cursor": "next"})
    assert result["observed_volume_usd"] == "15.30"
    assert result["reported_realized_pnl_usd"] == "-2"
    assert result["reported_fees_usd"] == "0.15"
    assert result["more_positions"] is True
    assert "account-wide" in result["scope"]
    rows[1]["total_traded_dollars"] = "NaN"
    assert kalshi_observed_capital({"market_positions": rows})["observed_volume_usd"] is None
    rows[1]["total_traded_dollars"] = "-1"
    assert kalshi_observed_capital({"market_positions": rows})["observed_volume_usd"] is None
    rows[1].pop("fees_paid_dollars")
    assert kalshi_observed_capital({"market_positions": rows})["reported_fees_usd"] is None


def test_missing_empty_or_duplicate_position_pages_do_not_claim_zero_volume():
    for payload in ({}, {"market_positions": []}, {"market_positions": [None]},
                    {"market_positions": [{"ticker": "A"}, {"ticker": "A"}]}):
        assert kalshi_observed_capital(payload)["observed_volume_usd"] is None


def test_empty_complete_position_page_is_authoritative_zero_but_incomplete_is_unknown():
    complete = kalshi_observed_capital({
        "market_positions": [], "pagination": {"complete": True, "pages": 1},
    })
    incomplete = kalshi_observed_capital({
        "market_positions": [], "pagination": {"complete": False, "pages": 1},
    })
    assert complete["observed_volume_usd"] == "0"
    assert complete["reported_exposure_usd"] == "0"
    assert complete["positions_complete"] is True
    assert incomplete["observed_volume_usd"] is None
    assert incomplete["more_positions"] is True


def test_native_valuation_uses_official_quotes_and_never_values_missing_reads(monkeypatch):
    import asyncio

    import httpx

    from noema.account_capital import value_native_wallets

    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"data": {"base": "SOL", "currency": "USD", "amount": "100"}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("noema.account_capital.httpx.AsyncClient", lambda **kwargs: client)
    rows = asyncio.run(value_native_wallets([
        {"chain": "solana", "readable": True, "sol": "0.25"},
        {"chain": "base", "readable": True, "native_balance": "0"},
        {"chain": "bitcoin", "readable": False, "btc": None},
    ]))
    assert rows[0]["native_value_usd"] == "25.00"
    assert rows[0]["native_valuation"]["source"] == "Coinbase public spot"
    assert rows[1]["native_value_usd"] == "0"
    assert rows[2]["native_value_usd"] is None
    assert requests == ["https://api.coinbase.com/v2/prices/SOL-USD/spot"]
