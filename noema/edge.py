from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class CostAssumptions:
    fee_fraction: float = 0.0
    slippage_fraction: float = 0.0
    uncertainty_haircut: float = 0.0

    @property
    def total(self) -> float:
        return self.fee_fraction + self.slippage_fraction + self.uncertainty_haircut


@dataclass(frozen=True)
class SideEdge:
    side: Side
    fair_probability: float
    executable_price: float
    raw_edge: float
    after_cost_edge: float


@dataclass(frozen=True)
class EdgeComparison:
    yes: SideEdge | None
    no: SideEdge | None
    best: SideEdge | None


def compare_binary_edges(
    *,
    probability_yes: float,
    yes_ask: float | None,
    no_ask: float | None,
    costs: CostAssumptions,
) -> EdgeComparison:
    if not 0 <= probability_yes <= 1:
        raise ValueError("probability_yes must be in [0, 1]")

    yes_edge: SideEdge | None = None
    no_edge: SideEdge | None = None

    if yes_ask is not None:
        raw = probability_yes - yes_ask
        yes_edge = SideEdge(
            side=Side.YES,
            fair_probability=probability_yes,
            executable_price=yes_ask,
            raw_edge=raw,
            after_cost_edge=raw - costs.total,
        )

    if no_ask is not None:
        probability_no = 1 - probability_yes
        raw = probability_no - no_ask
        no_edge = SideEdge(
            side=Side.NO,
            fair_probability=probability_no,
            executable_price=no_ask,
            raw_edge=raw,
            after_cost_edge=raw - costs.total,
        )

    candidates = [edge for edge in (yes_edge, no_edge) if edge is not None]
    best = max(candidates, key=lambda x: x.after_cost_edge, default=None)
    return EdgeComparison(yes=yes_edge, no=no_edge, best=best)
