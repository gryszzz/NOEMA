from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist, mean, pstdev


@dataclass(frozen=True)
class SharpeAudit:
    sharpe: float
    probabilistic_sharpe: float
    observations: int


@dataclass(frozen=True)
class PBOAudit:
    probability_backtest_overfit: float
    splits: int
    selected_oos_percentiles: tuple[float, ...]


def _moments(values: Sequence[float]) -> tuple[float, float, float, float]:
    n = len(values)
    if n < 3:
        raise ValueError("at least three returns are required")
    mu = mean(values)
    variance = sum((value - mu) ** 2 for value in values) / n
    if variance <= 0:
        return mu, 0.0, 0.0, 3.0
    sigma = math.sqrt(variance)
    skew = sum(((value - mu) / sigma) ** 3 for value in values) / n
    kurtosis = sum(((value - mu) / sigma) ** 4 for value in values) / n
    return mu, sigma, skew, kurtosis


def probabilistic_sharpe_ratio(
    returns: Sequence[float],
    *,
    benchmark_sharpe: float = 0.0,
) -> SharpeAudit:
    """Bailey/Lopez de Prado PSR using per-observation (not annualized) Sharpe."""

    mu, sigma, skew, kurtosis = _moments(returns)
    if sigma <= 0:
        sharpe = 0.0
        probability = 0.5 if mu == 0 else float(mu > 0)
        return SharpeAudit(sharpe, probability, len(returns))

    sharpe = mu / sigma
    denominator_sq = (
        1.0
        - skew * sharpe
        + ((kurtosis - 1.0) / 4.0) * sharpe * sharpe
    )
    if denominator_sq <= 0:
        return SharpeAudit(sharpe, 0.0, len(returns))

    z = (
        (sharpe - benchmark_sharpe)
        * math.sqrt(len(returns) - 1)
        / math.sqrt(denominator_sq)
    )
    probability = NormalDist().cdf(z)
    return SharpeAudit(sharpe, probability, len(returns))


def _sharpe_like(values: Sequence[float]) -> float:
    if len(values) < 2:
        return float("-inf")
    sigma = pstdev(values)
    if sigma <= 0:
        avg = mean(values)
        if avg > 0:
            return float("inf")
        if avg < 0:
            return float("-inf")
        return 0.0
    return mean(values) / sigma


def _slice_indices(length: int, slices: int) -> list[tuple[int, ...]]:
    if slices < 4 or slices % 2:
        raise ValueError("slices must be an even integer >= 4")
    if length < slices:
        raise ValueError("not enough observations for requested slices")

    base, remainder = divmod(length, slices)
    result: list[tuple[int, ...]] = []
    start = 0
    for index in range(slices):
        size = base + (1 if index < remainder else 0)
        result.append(tuple(range(start, start + size)))
        start += size
    return result


def probability_of_backtest_overfitting(
    returns_by_strategy: Mapping[str, Sequence[float]],
    *,
    slices: int = 8,
) -> PBOAudit:
    """Combinatorial symmetric cross-validation diagnostic.

    PBO is the fraction of splits where the in-sample winner ranks in the lower half
    of strategies out of sample. It is a research diagnostic, not a profitability claim.
    """

    if len(returns_by_strategy) < 2:
        raise ValueError("at least two strategies are required")
    lengths = {len(values) for values in returns_by_strategy.values()}
    if len(lengths) != 1:
        raise ValueError("all strategies must have equal observation counts")

    length = lengths.pop()
    partitions = _slice_indices(length, slices)
    half = slices // 2
    names = sorted(returns_by_strategy)

    percentiles: list[float] = []
    # Symmetric train/test pairs are duplicated if every combination is used.
    # Keep one representative by requiring slice 0 in the train side.
    for train_slices in combinations(range(slices), half):
        if 0 not in train_slices:
            continue
        train_set = set(train_slices)
        test_slices = tuple(index for index in range(slices) if index not in train_set)
        train_idx = [i for group in train_slices for i in partitions[group]]
        test_idx = [i for group in test_slices for i in partitions[group]]

        is_scores: dict[str, float] = {}
        oos_scores: dict[str, float] = {}
        for name in names:
            values = returns_by_strategy[name]
            is_scores[name] = _sharpe_like([values[i] for i in train_idx])
            oos_scores[name] = _sharpe_like([values[i] for i in test_idx])

        winner = max(names, key=lambda name: (is_scores[name], name))
        chosen = oos_scores[winner]
        below_or_equal = sum(score <= chosen for score in oos_scores.values())
        percentile = below_or_equal / len(names)
        percentiles.append(percentile)

    pbo = sum(percentile <= 0.5 for percentile in percentiles) / len(percentiles)
    return PBOAudit(
        probability_backtest_overfit=pbo,
        splits=len(percentiles),
        selected_oos_percentiles=tuple(percentiles),
    )
