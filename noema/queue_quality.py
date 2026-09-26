from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class QueueQuality:
    contracts_ahead: Decimal
    own_remaining: Decimal
    queue_ratio: Decimal | None
    score: float


def assess_queue_quality(
    *,
    contracts_ahead: Decimal,
    own_remaining: Decimal,
) -> QueueQuality:
    if contracts_ahead < 0 or own_remaining < 0:
        raise ValueError("queue values must be non-negative")
    ratio = None
    if own_remaining > 0:
        ratio = contracts_ahead / own_remaining
    # Diagnostic only: score decays as queue ahead grows relative to own order.
    score = 1.0 if ratio is None else float(Decimal(1) / (Decimal(1) + ratio))
    return QueueQuality(
        contracts_ahead=contracts_ahead,
        own_remaining=own_remaining,
        queue_ratio=ratio,
        score=max(0.0, min(score, 1.0)),
    )
