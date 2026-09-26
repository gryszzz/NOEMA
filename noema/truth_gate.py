from __future__ import annotations

from dataclasses import dataclass

from .grounding import GroundingResult
from .timing_gate import TimingDecision


@dataclass(frozen=True)
class TruthTimingDecision:
    eligible: bool
    reasons: tuple[str, ...]


def combine_truth_and_timing(
    grounding: GroundingResult,
    timing: TimingDecision,
) -> TruthTimingDecision:
    reasons = (*grounding.reasons, *timing.reasons)
    return TruthTimingDecision(
        eligible=grounding.grounded and timing.eligible,
        reasons=reasons,
    )
