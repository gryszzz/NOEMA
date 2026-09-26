"""Separate a forecasting benchmark from the price paid to buy YES."""

from __future__ import annotations

import math

from .models import MarketSnapshot


def yes_midpoint(market: MarketSnapshot) -> float | None:
    """Return a quote midpoint only when both YES sides form a valid book."""
    bid, ask = market.yes_bid, market.yes_ask
    if bid is None or ask is None:
        return None
    if not all(math.isfinite(value) for value in (bid, ask)) or not 0 <= bid <= ask <= 1:
        return None
    return (bid + ask) / 2


def snapshot_midpoint(snapshot: dict[str, object]) -> float | None:
    try:
        bid = float(snapshot["yes_bid"])
        ask = float(snapshot["yes_ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (bid, ask)) or not 0 <= bid <= ask <= 1:
        return None
    return (bid + ask) / 2
