from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeDecay:
    initial_edge: float
    half_life_steps: float | None
    persistence_fraction: float


def estimate_edge_decay(edges: list[float]) -> EdgeDecay:
    if not edges:
        return EdgeDecay(0.0, None, 0.0)

    initial = max(edges[0], 0.0)
    if initial == 0:
        return EdgeDecay(0.0, None, 0.0)

    positive = [max(edge, 0.0) for edge in edges]
    half = initial / 2
    half_step: float | None = None

    for index in range(1, len(positive)):
        previous = positive[index - 1]
        current = positive[index]
        if current <= half <= previous and previous != current:
            fraction = (previous - half) / (previous - current)
            half_step = (index - 1) + fraction
            break

    persistence = sum(edge > 0 for edge in positive) / len(positive)
    return EdgeDecay(
        initial_edge=initial,
        half_life_steps=half_step,
        persistence_fraction=persistence,
    )
