from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from .autonomy import earned_autonomy
from .autonomy_transition import AutonomyTransition, transition_autonomy
from .economic_ledger import EconomicLedger
from .economic_models import (
    AutonomyEvidence,
    AutonomyLevel,
    CapitalBucket,
    EconomicSnapshot,
    ProfitAllocation,
)
from .economic_policy import LevelLimits, limits_for_level
from .profit_waterfall import ProfitWaterfallPolicy, allocate_profit


@dataclass(frozen=True)
class EconomicReview:
    evidence_level: AutonomyLevel
    transition: AutonomyTransition
    limits: LevelLimits
    profit_plan: ProfitAllocation
    expansion_budget_available: bool


class EconomicController:
    """Coordinates economic policy without moving funds itself."""

    def __init__(
        self,
        ledger: EconomicLedger,
        *,
        waterfall_policy: ProfitWaterfallPolicy | None = None,
    ) -> None:
        self.ledger = ledger
        self.waterfall_policy = waterfall_policy or ProfitWaterfallPolicy()

    def review(
        self,
        snapshot: EconomicSnapshot,
        evidence: AutonomyEvidence,
        *,
        current_level: AutonomyLevel,
    ) -> EconomicReview:
        earned = earned_autonomy(evidence, current_level=current_level)
        transition = transition_autonomy(current_level, earned.earned_level)
        limits = limits_for_level(transition.next_level)
        profit_plan = allocate_profit(snapshot, policy=self.waterfall_policy)

        review = EconomicReview(
            evidence_level=earned.earned_level,
            transition=transition,
            limits=limits,
            profit_plan=profit_plan,
            expansion_budget_available=(
                transition.next_level
                in {AutonomyLevel.SELF_FUNDED, AutonomyLevel.EXPANSION}
                and profit_plan.allocations.get(
                    CapitalBucket.INFRASTRUCTURE,
                    Decimal(0),
                )
                > 0
            ),
        )
        self.ledger.append_event(
            "economic_review",
            payload={
                "evidence": {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in asdict(evidence).items()
                },
                "current_level": current_level.value,
                "evidence_level": earned.earned_level.value,
                "next_level": transition.next_level.value,
                "transition_reason": transition.reason,
                "profit_plan": {
                    bucket.value: str(amount)
                    for bucket, amount in profit_plan.allocations.items()
                },
            },
        )
        return review
