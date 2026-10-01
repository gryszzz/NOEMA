"""Strict display projections of official account observations, never execution inputs."""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def kalshi_cash_usd(balance: dict[str, Any]) -> str | None:
    # Official balance endpoint: prefer decimal dollars; legacy balance is cents.
    # https://docs.kalshi.com/api-reference/portfolio/get-balance
    if "balance_dollars" in balance:
        amount = _decimal(balance["balance_dollars"])
    else:
        cents = _decimal(balance.get("balance"))
        amount = None if cents is None else cents / 100
    return None if amount is None else str(amount)


def kalshi_observed_capital(positions: dict[str, Any]) -> dict[str, Any]:
    """Bounded account-level position totals; never imply all-time or agent attribution."""
    rows = positions.get("market_positions")
    pagination = positions.get("pagination") if isinstance(positions.get("pagination"), dict) else {}
    complete = pagination.get("complete") is True
    valid = isinstance(rows, list) and all(
        isinstance(row, dict) and row.get("ticker") for row in rows
    )
    if valid:
        valid = len({row["ticker"] for row in rows}) == len(rows)

    def total(field: str, *, signed: bool = False) -> str | None:
        if not valid or (not rows and not complete):
            return None
        if not rows:
            return "0"
        values = [_decimal(row.get(field)) for row in rows]
        if any(value is None or (not signed and value < 0) for value in values):
            return None
        return str(sum(values, Decimal(0)))

    return {
        "observed_volume_usd": total("total_traded_dollars"),
        "reported_realized_pnl_usd": total("realized_pnl_dollars", signed=True),
        "reported_fees_usd": total("fees_paid_dollars"),
        "reported_exposure_usd": total("market_exposure_dollars", signed=True),
        "position_rows": len(rows) if isinstance(rows, list) else None,
        "more_positions": (not complete if pagination else bool(positions.get("cursor"))),
        "positions_complete": complete,
        "position_pages": pagination.get("pages"),
        "scope": "Reported Kalshi position rows · account-wide · no fixed time window",
        "positions": [{key: row.get(key) for key in (
            "ticker", "position_fp", "total_traded_dollars", "realized_pnl_dollars",
            "market_exposure_dollars", "fees_paid_dollars", "last_updated_ts",
        )} for row in rows[:100] if isinstance(row, dict)] if isinstance(rows, list) else [],
    }


async def value_native_wallets(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Indicative native-asset marks; no token guesses and no ledger profit entries.

    Official public spot endpoint (no credentials):
    https://docs.cdp.coinbase.com/coinbase-business/track-apis/prices
    """
    symbols = {"solana": "SOL", "ethereum": "ETH", "base": "ETH",
               "arbitrum": "ETH", "optimism": "ETH", "polygon": "POL",
               "bnb-chain": "BNB", "avalanche": "AVAX", "bitcoin": "BTC"}
    needed = set()
    for row in networks:
        amount = _decimal(row.get("sol", row.get("native_balance", row.get("btc"))))
        if row.get("readable") is True and amount is not None and amount > 0:
            symbol = row.get("native_price_symbol") or symbols.get(row.get("chain"))
            if isinstance(symbol, str) and re.fullmatch(r"[A-Z][A-Z0-9]{0,11}", symbol):
                needed.add(symbol)
    async with httpx.AsyncClient(timeout=3.0) as client:
        async def quote(symbol: str):
            try:
                response = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot")
                response.raise_for_status()
                data = response.json().get("data", {})
                price = _decimal(data.get("amount"))
                if (data.get("currency") != "USD" or data.get("base", symbol) != symbol
                        or price is None or price <= 0):
                    return symbol, None
                return symbol, {"price_usd": str(price),
                                "observed_at": datetime.now(UTC).isoformat(),
                                "source": "Coinbase public spot"}
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                return symbol, None
        quotes = dict(await asyncio.gather(*(quote(symbol) for symbol in sorted(needed))))
    result = []
    for original in networks:
        row = dict(original)
        amount = _decimal(row.get("sol", row.get("native_balance", row.get("btc"))))
        price_symbol = row.get("native_price_symbol") or symbols.get(row.get("chain"))
        mark = quotes.get(price_symbol)
        usable = row.get("readable") is True and amount is not None and amount >= 0
        row["native_value_usd"] = (
            "0" if usable and amount == 0 else
            str(amount * Decimal(mark["price_usd"])) if usable and mark else None
        )
        row["native_price_symbol"] = price_symbol
        row["native_valuation"] = mark
        result.append(row)
    return result
