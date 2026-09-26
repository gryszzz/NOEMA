from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntryInputs:
    after_cost_edge: float
    spread: float
    depth_imbalance: float | None
    feed_latency_ms: float
    edge_persistence_ms: float
    price_change_last_second: float | None


@dataclass(frozen=True)
class EntryQuality:
    score: float
    components: dict[str, float]
    eligible: bool
    reasons: tuple[str, ...]


def assess_entry_quality(inputs: EntryInputs) -> EntryQuality:
    reasons: list[str] = []

    if inputs.after_cost_edge <= 0:
        reasons.append("non-positive after-cost edge")
    if not 0 <= inputs.spread <= 1:
        reasons.append("invalid spread")
    if inputs.feed_latency_ms < 0:
        reasons.append("negative feed latency")
    if inputs.edge_persistence_ms < 0:
        reasons.append("negative edge persistence")

    edge_component = max(0.0, min(inputs.after_cost_edge / 0.10, 1.0))
    spread_component = max(0.0, 1 - min(inputs.spread / 0.08, 1.0))
    latency_component = max(0.0, 1 - min(inputs.feed_latency_ms / 1000.0, 1.0))
    persistence_component = min(max(inputs.edge_persistence_ms / 2000.0, 0.0), 1.0)

    imbalance_component = 0.5
    if inputs.depth_imbalance is not None:
        imbalance_component = 1 - min(abs(inputs.depth_imbalance), 1.0) * 0.25

    shock_component = 1.0
    if inputs.price_change_last_second is not None:
        shock_component = max(
            0.0,
            1 - min(abs(inputs.price_change_last_second) / 0.10, 1.0),
        )
        if abs(inputs.price_change_last_second) >= 0.08:
            reasons.append("price shock too large for stable entry")

    components = {
        "edge": edge_component,
        "spread": spread_component,
        "latency": latency_component,
        "persistence": persistence_component,
        "depth_stability": imbalance_component,
        "shock_stability": shock_component,
    }

    score = (
        0.30 * edge_component
        + 0.20 * spread_component
        + 0.15 * latency_component
        + 0.20 * persistence_component
        + 0.05 * imbalance_component
        + 0.10 * shock_component
    )

    if score < 0.55:
        reasons.append("entry quality below threshold")

    return EntryQuality(
        score=score,
        components=components,
        eligible=not reasons,
        reasons=tuple(reasons),
    )
