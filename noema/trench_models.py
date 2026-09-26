from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TokenControlState:
    """Static/control-plane facts that can make a token unsafe to automate.

    None means the upstream source did not establish the fact. Unknown must never be
    silently converted into a safe value.
    """

    token_program: str | None = None
    mint_authority_present: bool | None = None
    freeze_authority_present: bool | None = None
    permanent_delegate_present: bool | None = None
    transfer_hook_present: bool | None = None
    transfer_fee_bps: int | None = None
    suspicious_flag: bool = False

    def __post_init__(self) -> None:
        if self.transfer_fee_bps is not None and self.transfer_fee_bps < 0:
            raise ValueError("transfer_fee_bps must be non-negative")


@dataclass(frozen=True)
class LaunchTick:
    """One immutable observation of a newly tradeable token.

    Volume fields should be life-to-date/cumulative over the launch when the source
    permits it. Provider-specific participant metrics are kept separate.
    """

    observed_at: datetime
    price_usd: float
    liquidity_usd: float
    buy_volume_usd: float
    sell_volume_usd: float
    unique_buyers: int | None = None
    unique_sellers: int | None = None
    organic_net_buyers: int | None = None
    total_traders: int | None = None
    net_buyers: int | None = None
    organic_buy_volume_usd: float | None = None
    organic_sell_volume_usd: float | None = None
    holder_shares: tuple[float, ...] = ()
    creator_supply_fraction: float | None = None
    organic_score: float | None = None
    wash_trade_probability: float | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.price_usd < 0 or self.liquidity_usd < 0:
            raise ValueError("price and liquidity must be non-negative")
        if self.buy_volume_usd < 0 or self.sell_volume_usd < 0:
            raise ValueError("volumes must be non-negative")
        for name in (
            "organic_buy_volume_usd",
            "organic_sell_volume_usd",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in (
            "unique_buyers",
            "unique_sellers",
            "organic_net_buyers",
            "total_traders",
            "net_buyers",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
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
    buyer_growth_fraction: float | None
    buyer_acceleration: float | None
    participation_balance: float | None
    organic_buyer_share: float | None
    organic_volume_fraction: float | None
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
