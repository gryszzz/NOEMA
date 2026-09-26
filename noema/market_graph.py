from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbabilityNode:
    key: str
    probability: float


@dataclass(frozen=True)
class GraphViolation:
    kind: str
    magnitude: float
    detail: str


def check_exhaustive_partition(
    nodes: list[ProbabilityNode],
    *,
    tolerance: float = 0.02,
) -> list[GraphViolation]:
    if not nodes:
        return []
    for node in nodes:
        if not 0 <= node.probability <= 1:
            raise ValueError("probabilities must be in [0, 1]")

    total = sum(node.probability for node in nodes)
    gap = abs(total - 1.0)
    if gap <= tolerance:
        return []
    return [
        GraphViolation(
            kind="partition_sum",
            magnitude=gap,
            detail=f"partition sums to {total:.4f} instead of 1.0",
        )
    ]


def check_monotonic_thresholds(
    thresholds: list[tuple[float, float]],
    *,
    tolerance: float = 1e-9,
) -> list[GraphViolation]:
    """Check P(X >= higher threshold) <= P(X >= lower threshold)."""
    ordered = sorted(thresholds)
    violations: list[GraphViolation] = []
    for index in range(1, len(ordered)):
        prev_threshold, prev_probability = ordered[index - 1]
        threshold, probability = ordered[index]
        if probability > prev_probability + tolerance:
            violations.append(
                GraphViolation(
                    kind="monotonicity",
                    magnitude=probability - prev_probability,
                    detail=(
                        f"P(X >= {threshold})={probability:.4f} exceeds "
                        f"P(X >= {prev_threshold})={prev_probability:.4f}"
                    ),
                )
            )
    return violations
