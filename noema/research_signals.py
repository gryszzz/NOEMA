from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FlowWindow:
    buy_volume: float
    sell_volume: float
    price_change: float
    trades: int


@dataclass(frozen=True)
class ToxicitySignal:
    imbalance: float
    directional_pressure: float
    toxicity_score: float
    classification: str


def assess_flow_toxicity(window: FlowWindow) -> ToxicitySignal:
    if window.buy_volume < 0 or window.sell_volume < 0:
        raise ValueError("volumes must be non-negative")
    if window.trades < 0:
        raise ValueError("trades must be non-negative")

    total = window.buy_volume + window.sell_volume
    imbalance = 0.0 if total == 0 else abs(window.buy_volume - window.sell_volume) / total

    signed_flow = 0.0 if total == 0 else (window.buy_volume - window.sell_volume) / total
    directional_pressure = min(1.0, abs(signed_flow) * (1 + min(abs(window.price_change) / 0.10, 1.0)))

    # Research diagnostic inspired by VPIN/order-flow toxicity ideas.
    # It is intentionally simple and must be validated empirically before use.
    activity = min(window.trades / 50.0, 1.0)
    toxicity = min(1.0, 0.55 * imbalance + 0.30 * directional_pressure + 0.15 * activity)

    if toxicity >= 0.75:
        classification = "high"
    elif toxicity >= 0.50:
        classification = "elevated"
    elif toxicity >= 0.25:
        classification = "moderate"
    else:
        classification = "low"

    return ToxicitySignal(
        imbalance=imbalance,
        directional_pressure=directional_pressure,
        toxicity_score=toxicity,
        classification=classification,
    )


@dataclass(frozen=True)
class QueueResearch:
    contracts_ahead: float
    own_size: float
    queue_ratio: float | None
    crowding_score: float


def assess_queue_crowding(*, contracts_ahead: float, own_size: float) -> QueueResearch:
    if contracts_ahead < 0 or own_size < 0:
        raise ValueError("queue values must be non-negative")
    if own_size == 0:
        return QueueResearch(contracts_ahead, own_size, None, 1.0 if contracts_ahead > 0 else 0.0)
    ratio = contracts_ahead / own_size
    crowding = min(1.0, ratio / 20.0)
    return QueueResearch(contracts_ahead, own_size, ratio, crowding)


@dataclass(frozen=True)
class ResolutionPressure:
    seconds_to_resolution: float
    normalized_pressure: float


def time_to_resolution_pressure(seconds_to_resolution: float) -> ResolutionPressure:
    if seconds_to_resolution < 0:
        raise ValueError("seconds_to_resolution must be non-negative")
    # Pressure rises non-linearly inside the final hour.
    if seconds_to_resolution >= 3600:
        score = 0.0
    else:
        score = 1 - (seconds_to_resolution / 3600) ** 0.5
    return ResolutionPressure(seconds_to_resolution, max(0.0, min(score, 1.0)))
