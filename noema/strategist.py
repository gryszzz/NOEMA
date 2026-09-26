from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean


_EPSILON = 1e-9


def _clip_probability(value: float) -> float:
    return min(max(value, _EPSILON), 1 - _EPSILON)


def logit(p: float) -> float:
    p = _clip_probability(p)
    return math.log(p / (1 - p))


def logistic(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


@dataclass(frozen=True)
class ModelBelief:
    name: str
    probability_yes: float
    reliability: float
    sample_size: int = 0


@dataclass(frozen=True)
class EnsembleBelief:
    probability_yes: float
    lower_bound: float
    upper_bound: float
    disagreement: float
    effective_models: float
    contributors: tuple[str, ...]


@dataclass(frozen=True)
class EnsemblePolicy:
    market_prior_weight: float = 2.0
    min_model_weight: float = 0.05
    max_model_weight: float = 3.0
    sample_size_half_life: float = 100.0
    disagreement_multiplier: float = 1.25
    base_interval_width: float = 0.04
    max_interval_width: float = 0.25


class BayesianEnsembler:
    """Reliability-weighted log-odds pooling with shrinkage to market prior.

    The ensemble intentionally shrinks toward the market when specialist evidence
    is weak. Reliability weights are bounded and discounted for small samples.
    """

    def __init__(self, policy: EnsemblePolicy | None = None) -> None:
        self.policy = policy or EnsemblePolicy()

    def combine(
        self,
        *,
        market_probability: float,
        beliefs: Iterable[ModelBelief],
    ) -> EnsembleBelief:
        rows = list(beliefs)
        if not rows:
            p = _clip_probability(market_probability)
            return EnsembleBelief(
                probability_yes=p,
                lower_bound=max(0.0, p - self.policy.base_interval_width),
                upper_bound=min(1.0, p + self.policy.base_interval_width),
                disagreement=0.0,
                effective_models=0.0,
                contributors=(),
            )

        weighted_logits: list[tuple[float, float]] = []
        probabilities: list[float] = []
        names: list[str] = []

        for belief in rows:
            if not 0 <= belief.probability_yes <= 1:
                raise ValueError("model probabilities must be in [0, 1]")
            sample_discount = 1 - math.exp(
                -max(0, belief.sample_size) / self.policy.sample_size_half_life
            )
            raw_weight = belief.reliability * max(sample_discount, self.policy.min_model_weight)
            weight = min(max(raw_weight, self.policy.min_model_weight), self.policy.max_model_weight)
            weighted_logits.append((logit(belief.probability_yes), weight))
            probabilities.append(belief.probability_yes)
            names.append(belief.name)

        prior_weight = max(0.0, self.policy.market_prior_weight)
        numerator = prior_weight * logit(market_probability)
        denominator = prior_weight

        for model_logit, weight in weighted_logits:
            numerator += model_logit * weight
            denominator += weight

        pooled = logistic(numerator / max(denominator, _EPSILON))
        disagreement = 0.0
        if len(probabilities) > 1:
            disagreement = math.sqrt(
                mean((p - mean(probabilities)) ** 2 for p in probabilities)
            )

        effective_models = sum(weight for _, weight in weighted_logits)
        half_width = (
            self.policy.base_interval_width
            + self.policy.disagreement_multiplier * disagreement
            + 0.06 / math.sqrt(max(effective_models, 1.0))
        )
        half_width = min(half_width, self.policy.max_interval_width)

        return EnsembleBelief(
            probability_yes=pooled,
            lower_bound=max(0.0, pooled - half_width),
            upper_bound=min(1.0, pooled + half_width),
            disagreement=disagreement,
            effective_models=effective_models,
            contributors=tuple(names),
        )
