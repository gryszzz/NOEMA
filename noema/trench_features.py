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


def _buyer_count(tick: LaunchTick) -> int | None:
    if tick.organic_net_buyers is not None:
        return tick.organic_net_buyers
    return tick.unique_buyers


def _buyer_acceleration(ticks: Sequence[LaunchTick]) -> float | None:
    if len(ticks) < 3:
        return None
    counts = [_buyer_count(tick) for tick in ticks[-3:]]
    if any(value is None for value in counts):
        return None
    first, middle, last = (int(value) for value in counts)
    first_delta = max(0, middle - first)
    last_delta = max(0, last - middle)
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


def _buyer_growth(first: LaunchTick, last: LaunchTick) -> float | None:
    start = _buyer_count(first)
    end = _buyer_count(last)
    if start is None or end is None:
        return None
    return _fraction_change(float(max(start, 1)), float(end))


def _participation_balance(last: LaunchTick) -> float | None:
    if last.unique_buyers is None or last.unique_sellers is None:
        return None
    participants = last.unique_buyers + last.unique_sellers
    if participants <= 0:
        return 0.0
    return 1.0 - abs(last.unique_buyers - last.unique_sellers) / participants


def _organic_buyer_share(last: LaunchTick) -> float | None:
    if last.organic_net_buyers is None or last.total_traders is None:
        return None
    if last.total_traders <= 0:
        return 0.0
    return max(0.0, min(1.0, last.organic_net_buyers / last.total_traders))


def _organic_volume_fraction(last: LaunchTick) -> float | None:
    if last.organic_buy_volume_usd is None or last.organic_sell_volume_usd is None:
        return None
    total = last.buy_volume_usd + last.sell_volume_usd
    if total <= 0:
        return 0.0
    organic = last.organic_buy_volume_usd + last.organic_sell_volume_usd
    return max(0.0, min(1.0, organic / total))


def extract_trench_features(ticks: Sequence[LaunchTick]) -> TrenchFeatures:
    """Create an auditable early-life feature vector.

    The collector stores life-to-date launch volume where possible, so flow imbalance uses
    the latest cumulative snapshot rather than summing snapshots and double-counting volume.
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

    top, top5, hhi = _holder_stats(last.holder_shares)
    features = TrenchFeatures(
        age_seconds=age,
        observations=len(ordered),
        return_fraction=_fraction_change(first.price_usd, last.price_usd),
        max_drawdown_fraction=_max_drawdown([tick.price_usd for tick in ordered]),
        liquidity_growth_fraction=_fraction_change(first.liquidity_usd, last.liquidity_usd),
        signed_flow_imbalance=_flow_imbalance(
            last.buy_volume_usd,
            last.sell_volume_usd,
        ),
        buyer_growth_fraction=_buyer_growth(first, last),
        buyer_acceleration=_buyer_acceleration(ordered),
        participation_balance=_participation_balance(last),
        organic_buyer_share=_organic_buyer_share(last),
        organic_volume_fraction=_organic_volume_fraction(last),
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
        features.organic_buyer_share,
        features.organic_volume_fraction,
    )
    if not all(isfinite(value) for value in numeric if value is not None):
        raise ValueError("non-finite launch feature")
    return features
