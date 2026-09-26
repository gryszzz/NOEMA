from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TokenControlState:
    """Static/control-plane facts that can make a token unsafe to automate."""

    token_program: str = "spl-token"
    mint_authority_present: bool = False
    freeze_authority_present: bool = False
    permanent_delegate_present: bool = False
    transfer_hook_present: bool = False
    transfer_fee_bps: int = 0

    def __post_init__(self) -> None:
        if self.transfer_fee_bps < 0:
            raise ValueError("transfer_fee_bps must be non-negative")


@dataclass(frozen=True)
class LaunchTick:
    """One immutable observation of a newly tradeable token."""

    observed_at: datetime
    price_usd: float
    liquidity_usd: float
    buy_volume_usd: float
    sell_volume_usd: float
    unique_buyers: int
    unique_sellers: int
    holder_shares: tuple[float, ...] = ()
    creator_supply_fraction: float | None = None
    organic_score: float | None = None
    wash_trade_probability: float | None = None

    def __post_init__(self) -> None:
        if self.price_usd < 0 or self.liquidity_usd < 0:
            raise ValueError("price and liquidity must be non-negative")
        if self.buy_volume_usd < 0 or self.sell_volume_usd < 0:
            raise ValueError("volumes must be non-negative")
        if self.unique_buyers < 0 or self.unique_sellers < 0:
            raise ValueError("unique participant counts must be non-negative")
        if any(not 0 <= share <= 1 for share in self.holder_shares):
            raise ValueError("holder shares must be in [0, 1]")
        if self.creator_supply_fraction is not None and not 0 <= self.creator_supply_fraction <= 1:
            raise ValueError("creator_supply_fraction must be in [0, 1]")
        if self.organic_score is not None and not 0 <= self.organic_score <= 100:
            raise ValueError("organic_score must be in [0, 100]")
        if self.wash_trade_probability is not None and not 0 <= self.wash_trade_probability <= 1:
            raise ValueError("wash_trade_probability must be in [0, 1]")


@dataclass(frozen=True)
class TrenchFeatures:
    age_seconds: float
    observations: int
    return_fraction: float
    max_drawdown_fraction: float
    liquidity_growth_fraction: float
    signed_flow_imbalance: float
    buyer_growth_fraction: float
    buyer_acceleration: float
    participation_balance: float
    top_holder_fraction: float | None
    top5_holder_fraction: float | None
    holder_hhi: float | None
    creator_supply_fraction: float | None
    organic_score: float | None
    wash_trade_probability: float | None


@dataclass(frozen=True)
class TrenchAssessment:
    disposition: str
    survival_risk: float
    opportunity_score: float
    reasons: tuple[str, ...]
    features: TrenchFeatures
