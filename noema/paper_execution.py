"""Read-only, size-constrained YES taker quotes for paper research.

These are hypothetical immediate fills against an observed orderbook. They
cannot establish that an order would have filled after network latency.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from .risk import RiskPolicy

# Kalshi general prediction-market taker schedule, effective July 7, 2026.
# The current schedule must be rechecked before any future live implementation.
TAKER_RATE = Decimal("0.07")
SCHEDULE_VERSION = "kalshi-general-2026-07-07"
CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing or invalid decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("missing or invalid decimal") from exc
    if not number.is_finite():
        raise ValueError("non-finite decimal")
    return number


@dataclass(frozen=True)
class FeeTerms:
    fee_type: str
    taker_multiplier: Decimal
    source: str = "Kalshi series/event API"
    schedule_version: str = SCHEDULE_VERSION

    @classmethod
    def from_api(cls, series: dict[str, Any], event: dict[str, Any]) -> FeeTerms:
        if not isinstance(series, dict) or not isinstance(event, dict):
            raise TypeError("missing series or event fee metadata")
        fee_type = event.get("fee_type_override") or series.get("fee_type")
        raw_multiplier = event.get("fee_multiplier_override")
        if raw_multiplier is None:
            raw_multiplier = series.get("fee_multiplier")
        multiplier = _decimal(raw_multiplier)
        if fee_type != "quadratic" or multiplier < 0:
            raise ValueError("unsupported or invalid taker fee terms")
        return cls(fee_type, multiplier)


def taker_fee(price: Decimal, contracts: Decimal, terms: FeeTerms) -> Decimal:
    """Conservative cent rounding for a single price level.

    Kalshi specifies round up of fee plus position cost to a centicent. Keep
    that rounding, then conservatively round the fee up to a full cent.
    """
    if (terms.fee_type != "quadratic" or terms.taker_multiplier < 0
            or terms.schedule_version != SCHEDULE_VERSION):
        raise ValueError("unverified fee terms")
    if not 0 < price < 1 or contracts <= 0:
        raise ValueError("invalid contract price or count")
    raw = terms.taker_multiplier * TAKER_RATE * contracts * price * (1 - price)
    if raw == 0:
        return Decimal(0)
    position_cost = price * contracts
    kalshi_rounded = (position_cost + raw).quantize(
        CENTICENT, rounding=ROUND_CEILING,
    ) - position_cost
    return max(kalshi_rounded, raw.quantize(CENT, rounding=ROUND_CEILING))


@dataclass(frozen=True)
class PaperQuote:
    contracts: Decimal
    average_yes_price: Decimal
    entry_cost_usd: Decimal
    taker_fee_usd: Decimal
    total_debit_usd: Decimal
    expected_net_pnl_usd: Decimal
    best_yes_ask: Decimal
    best_yes_bid: Decimal
    schedule_version: str

    def as_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


def quote_yes_taker(
    orderbook: dict[str, Any], *, contracts: Decimal,
    probability_yes: float, fee_terms: FeeTerms,
) -> PaperQuote:
    """Walk NO bids (YES asks) and require enough visible depth for a full fill."""
    contracts = _decimal(contracts)
    if contracts <= 0 or contracts.as_tuple().exponent < -2:
        raise ValueError("contracts must be positive with at most 2 decimal places")
    if not isinstance(probability_yes, int | float) or isinstance(probability_yes, bool):
        raise TypeError("invalid independent probability")
    if not math.isfinite(probability_yes) or not 0 <= probability_yes <= 1:
        raise ValueError("invalid independent probability")
    if not isinstance(orderbook, dict) or not isinstance(orderbook.get("orderbook_fp"), dict):
        raise TypeError("missing current orderbook")
    book = orderbook["orderbook_fp"]
    levels = book.get("no_dollars")
    yes_bids = book.get("yes_dollars")
    if not isinstance(levels, list) or not levels:
        raise ValueError("no visible YES asks")
    if not isinstance(yes_bids, list) or not yes_bids:
        raise ValueError("no visible YES bids for spread validation")
    bid_prices: list[Decimal] = []
    for level in yes_bids:
        if not isinstance(level, list | tuple) or len(level) != 2:
            raise ValueError("malformed YES bid level")
        bid, quantity = (_decimal(value) for value in level)
        if not 0 < bid < 1 or quantity <= 0:
            raise ValueError("invalid YES bid price or size")
        bid_prices.append(bid)
    asks: list[tuple[Decimal, Decimal]] = []
    seen: set[Decimal] = set()
    for level in levels:
        if not isinstance(level, list | tuple) or len(level) != 2:
            raise ValueError("malformed orderbook level")
        no_bid, quantity = (_decimal(value) for value in level)
        yes_ask = 1 - no_bid
        if not 0 < yes_ask < 1 or quantity <= 0 or yes_ask in seen:
            raise ValueError("invalid orderbook price or size")
        seen.add(yes_ask)
        asks.append((yes_ask, quantity))
    asks.sort(key=lambda row: row[0])
    best_yes_bid = max(bid_prices)
    if not 0 <= asks[0][0] - best_yes_bid <= Decimal(str(RiskPolicy().max_spread)):
        raise ValueError("YES spread outside paper risk policy")
    remaining, cost, fees = contracts, Decimal(0), Decimal(0)
    for price, visible in asks:
        count = min(remaining, visible)
        cost += price * count
        fees += taker_fee(price, count, fee_terms)
        remaining -= count
        if remaining == 0:
            break
    if remaining > 0:
        raise ValueError("insufficient visible YES ask depth for a full fill")
    return PaperQuote(
        contracts=contracts, average_yes_price=cost / contracts,
        entry_cost_usd=cost, taker_fee_usd=fees,
        total_debit_usd=cost + fees,
        expected_net_pnl_usd=Decimal(str(probability_yes)) * contracts - cost - fees,
        best_yes_ask=asks[0][0], best_yes_bid=best_yes_bid,
        schedule_version=fee_terms.schedule_version,
    )


def parse_aware_time(value: object) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError("missing timestamp") from exc
    if dt.tzinfo is None:
        raise ValueError("timestamp must have a timezone")
    return dt.astimezone(UTC)
