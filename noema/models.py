from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Decision(str, Enum):
    PASS = "pass"
    PAPER_BUY_YES = "paper_buy_yes"
    PAPER_BUY_NO = "paper_buy_no"
    LIVE_BUY_YES = "live_buy_yes"
    LIVE_BUY_NO = "live_buy_no"


@dataclass(frozen=True)
class MarketSnapshot:
    venue: str
    market_id: str
    title: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    liquidity_usd: float | None
    closes_at: datetime | None
    resolution_rules: str | None
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Forecast:
    market_id: str
    venue: str
    probability_yes: float
    lower_bound: float
    upper_bound: float
    model_version: str
    evidence_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Opportunity:
    forecast: Forecast
    snapshot: MarketSnapshot
    market_probability: float
    raw_edge: float
    estimated_cost: float
    uncertainty_penalty: float
    robust_edge: float


@dataclass(frozen=True)
class Action:
    decision: Decision
    market_id: str
    venue: str
    max_price: float | None
    stake_usd: float
    reason: str
