from __future__ import annotations

from dataclasses import dataclass

from .trench_models import TokenControlState, TrenchAssessment, TrenchFeatures


@dataclass(frozen=True)
class TrenchPolicy:
    """Research defaults, not claims of optimal or profitable thresholds."""

    max_transfer_fee_bps: int = 300
    max_top_holder_fraction: float = 0.20
    max_top5_holder_fraction: float = 0.55
    max_creator_supply_fraction: float = 0.10
    max_wash_trade_probability: float = 0.65
    min_organic_score: float = 25.0
    min_liquidity_usd: float = 5_000.0


def _cap01(value: float) -> float:
    return max(0.0, min(1.0, value))


def assess_trench_candidate(
    features: TrenchFeatures,
    control: TokenControlState,
    *,
    current_liquidity_usd: float,
    policy: TrenchPolicy | None = None,
) -> TrenchAssessment:
    """Rank a launch for research attention without creating a trade instruction."""

    policy = policy or TrenchPolicy()
    reasons: list[str] = []
    risk = 0.0

    if current_liquidity_usd < policy.min_liquidity_usd:
        reasons.append("liquidity below research floor")
        risk += 0.25
    if control.freeze_authority_present:
        reasons.append("freeze authority retained")
        risk += 0.30
    if control.mint_authority_present:
        reasons.append("mint authority retained")
        risk += 0.15
    if control.permanent_delegate_present:
        reasons.append("permanent delegate present")
        risk += 0.30
    if control.transfer_hook_present:
        reasons.append("transfer hook requires explicit review")
        risk += 0.15
    if control.transfer_fee_bps > policy.max_transfer_fee_bps:
        reasons.append("transfer fee above research policy")
        risk += 0.30

    if (
        features.top_holder_fraction is not None
        and features.top_holder_fraction > policy.max_top_holder_fraction
    ):
        reasons.append("largest holder concentration high")
        risk += 0.20
    if (
        features.top5_holder_fraction is not None
        and features.top5_holder_fraction > policy.max_top5_holder_fraction
    ):
        reasons.append("top-five holder concentration high")
        risk += 0.20
    if (
        features.creator_supply_fraction is not None
        and features.creator_supply_fraction > policy.max_creator_supply_fraction
    ):
        reasons.append("creator supply concentration high")
        risk += 0.25
    if (
        features.wash_trade_probability is not None
        and features.wash_trade_probability > policy.max_wash_trade_probability
    ):
        reasons.append("wash-trade risk elevated")
        risk += 0.35
    if (
        features.organic_score is not None
        and features.organic_score < policy.min_organic_score
    ):
        reasons.append("organic activity weak")
        risk += 0.15
    if features.liquidity_growth_fraction < -0.20:
        reasons.append("liquidity is contracting quickly")
        risk += 0.30
    if features.max_drawdown_fraction > 0.50:
        reasons.append("early drawdown exceeds 50%")
        risk += 0.25

    survival_risk = _cap01(risk)

    organic = 0.5 if features.organic_score is None else features.organic_score / 100.0
    liquidity_growth = _cap01(0.5 + features.liquidity_growth_fraction / 2)
    buyer_growth = _cap01(features.buyer_growth_fraction / 3)
    acceleration = _cap01((features.buyer_acceleration + 1) / 2)
    flow = _cap01((features.signed_flow_imbalance + 1) / 2)
    participation = _cap01(features.participation_balance)

    raw_opportunity = (
        0.25 * buyer_growth
        + 0.20 * acceleration
        + 0.20 * liquidity_growth
        + 0.15 * flow
        + 0.10 * participation
        + 0.10 * organic
    )
    opportunity_score = _cap01(raw_opportunity * (1.0 - 0.80 * survival_risk))

    hard_quarantine = (
        control.permanent_delegate_present
        or control.transfer_fee_bps > policy.max_transfer_fee_bps
        or (
            features.wash_trade_probability is not None
            and features.wash_trade_probability > policy.max_wash_trade_probability
        )
        or survival_risk >= 0.75
    )

    if hard_quarantine:
        disposition = "quarantine"
    elif opportunity_score >= 0.55 and survival_risk <= 0.35:
        disposition = "research_candidate"
    else:
        disposition = "observe"

    return TrenchAssessment(
        disposition=disposition,
        survival_risk=survival_risk,
        opportunity_score=opportunity_score,
        reasons=tuple(reasons),
        features=features,
    )
