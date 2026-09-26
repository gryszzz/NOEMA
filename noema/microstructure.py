from __future__ import annotations

from dataclasses import dataclass

from .orderbook_state import BinaryOrderBook


@dataclass(frozen=True)
class MicrostructureFeatures:
    yes_bid: float | None
    yes_ask: float | None
    spread: float | None
    midpoint: float | None
    depth_imbalance_5: float | None
    synchronized: bool


def extract_microstructure(book: BinaryOrderBook) -> MicrostructureFeatures:
    return MicrostructureFeatures(
        yes_bid=book.best_yes_bid,
        yes_ask=book.best_yes_ask,
        spread=book.spread_yes,
        midpoint=book.midpoint_yes,
        depth_imbalance_5=book.top_depth_imbalance(5),
        synchronized=book.synchronized,
    )
