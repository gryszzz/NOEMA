from __future__ import annotations

from collections.abc import Sequence
from math import isfinite

from .trench_models import LaunchTick, TrenchFeatures


def _fraction_change(start: float, end: float) -> float:
    if start <= 0:
        return 0.0
    return (end - start) / start


def _flow_imbalance(buy: float, sell: float) -> float:
    total = buy + sell
    return 0.0 if total <= 0 else (buy - sell) / total


def _max_drawdown(prices: Sequence[float]) -> float:
    peak = 0.0
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            worst = max(worst, (peak - price) / peak)
    return worst


def _buyer_acceleration(ticks: Sequence[LaunchTick]) -> float:
    if len(ticks) < 3:
        return 0.0
    first_delta = max(0, ticks[-2].unique_buyers - ticks[-3].unique_buyers)
    last_delta = max(0, ticks[-1].unique_buyers - ticks[-2].unique_buyers)
    scale = max(1, first_delta + last_delta)
    return max(-1.0, min(1.0, (last_delta - first_delta) / scale))


def _holder_stats(shares: tuple[float, ...]) -> tuple[float | None, float | None, float | None]:
    if not shares:
        return None, None, None
    ordered = sorted(shares, reverse=True)
    top = ordered[0]
    top5 = min(1.0, sum(ordered[:5]))
    hhi = min(1.0, sum(share * share for share in ordered))
    return top, top5, hhi


def extract_trench_features(ticks: Sequence[LaunchTick]) -> TrenchFeatures:
    """Create a small, auditable early-life feature vector.

    This function deliberately performs no profitability inference. It only summarizes
    observations so the same immutable inputs can be replayed by multiple research models.
    """

    if len(ticks) < 2:
        raise ValueError("at least two launch ticks are required")

    ordered = sorted(ticks, key=lambda tick: tick.observed_at)
    if len({tick.observed_at for tick in ordered}) != len(ordered):
        raise ValueError("launch tick timestamps must be unique")

    first = ordered[0]
    last = ordered[-1]
    age = (last.observed_at - first.observed_at).total_seconds()
    if age <= 0:
        raise ValueError("launch observations must advance in time")

    total_buy = sum(tick.buy_volume_usd for tick in ordered)
    total_sell = sum(tick.sell_volume_usd for tick in ordered)
    participants = last.unique_buyers + last.unique_sellers
    participation_balance = (
        0.0
        if participants <= 0
        else 1.0 - abs(last.unique_buyers - last.unique_sellers) / participants
    )

    top, top5, hhi = _holder_stats(last.holder_shares)
    features = TrenchFeatures(
        age_seconds=age,
        observations=len(ordered),
        return_fraction=_fraction_change(first.price_usd, last.price_usd),
        max_drawdown_fraction=_max_drawdown([tick.price_usd for tick in ordered]),
        liquidity_growth_fraction=_fraction_change(first.liquidity_usd, last.liquidity_usd),
        signed_flow_imbalance=_flow_imbalance(total_buy, total_sell),
        buyer_growth_fraction=_fraction_change(
            float(max(first.unique_buyers, 1)),
            float(last.unique_buyers),
        ),
        buyer_acceleration=_buyer_acceleration(ordered),
        participation_balance=participation_balance,
        top_holder_fraction=top,
        top5_holder_fraction=top5,
        holder_hhi=hhi,
        creator_supply_fraction=last.creator_supply_fraction,
        organic_score=last.organic_score,
        wash_trade_probability=last.wash_trade_probability,
    )

    numeric = (
        features.age_seconds,
        features.return_fraction,
        features.max_drawdown_fraction,
        features.liquidity_growth_fraction,
        features.signed_flow_imbalance,
        features.buyer_growth_fraction,
        features.buyer_acceleration,
        features.participation_balance,
    )
    if not all(isfinite(value) for value in numeric):
        raise ValueError("non-finite launch feature")
    return features
