from __future__ import annotations

import math
from dataclasses import dataclass

from .specialists import SpecialistProfile, SpecialistState, specialist_attention_multiplier


@dataclass(frozen=True)
class AttentionAllocation:
    specialist: str
    family: str
    state: SpecialistState
    attention_fraction: float
    base_weight: float
    reason: str


@dataclass(frozen=True)
class EcosystemPlan:
    allocations: tuple[AttentionAllocation, ...]
    dominant_specialist: str | None
    exploration_fraction: float
    family_cap: float
    idle_fraction: float
    active_specialists: int
    quarantined_specialists: int


def _normalize(weights: dict[str, float], total: float) -> dict[str, float]:
    positive = {name: max(0.0, weight) for name, weight in weights.items() if weight > 0}
    denominator = sum(positive.values())
    if denominator <= 0 or total <= 0:
        return {name: 0.0 for name in weights}
    return {
        name: (positive.get(name, 0.0) / denominator) * total
        for name in weights
    }


def _family_totals(
    shares: dict[str, float],
    families: dict[str, str],
) -> dict[str, float]:
    totals: dict[str, float] = {}
    for name, share in shares.items():
        family = families[name]
        totals[family] = totals.get(family, 0.0) + share
    return totals


def _apply_family_cap(
    shares: dict[str, float],
    families: dict[str, str],
    *,
    cap: float,
) -> dict[str, float]:
    """Scale over-concentrated families down.

    Excess attention intentionally becomes idle rather than being forced into a weaker
    specialist merely to keep utilization at 100%.
    """

    totals = _family_totals(shares, families)
    adjusted = dict(shares)
    for family, total in totals.items():
        if total <= cap or total <= 0:
            continue
        scale = cap / total
        for name, specialist_family in families.items():
            if specialist_family == family:
                adjusted[name] *= scale
    return adjusted


def allocate_specialist_attention(
    profiles: list[SpecialistProfile],
    *,
    exploration_fraction: float = 0.15,
    family_cap: float = 0.65,
    negative_return_scale: float = 0.10,
) -> EcosystemPlan:
    """Allocate research attention across independent specialist families.

    This allocates *attention*, not capital or trade size. Quarantined specialists receive
    zero. Shadow specialists receive a bounded exploration pool. Established specialists
    compete for the remaining pool using their evidence-backed attention multiplier.
    """

    if not math.isfinite(exploration_fraction) or not 0 <= exploration_fraction <= 0.50:
        raise ValueError("exploration_fraction must be in [0, 0.50]")
    if not math.isfinite(family_cap) or not 0 < family_cap <= 1:
        raise ValueError("family_cap must be in (0, 1]")
    if not math.isfinite(negative_return_scale) or negative_return_scale <= 0:
        raise ValueError("negative_return_scale must be positive and finite")

    unique: dict[str, SpecialistProfile] = {}
    for profile in profiles:
        if not profile.name.strip():
            raise ValueError("specialist name cannot be empty")
        if not profile.family.strip() or not isinstance(profile.state, SpecialistState):
            raise ValueError("specialist family and state must be valid")
        if type(profile.resolved) is not int or profile.resolved < 0:
            raise ValueError("resolved must be a non-negative integer")
        for name in (
            "reliability", "calibration_error", "after_cost_return", "drawdown_fraction",
        ):
            value = getattr(profile, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite when available")
        if profile.reliability is None:
            raise ValueError("reliability must be finite")
        for name in ("calibration_error", "drawdown_fraction"):
            value = getattr(profile, name)
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if profile.name in unique:
            raise ValueError(f"duplicate specialist name: {profile.name}")
        unique[profile.name] = profile

    if not unique:
        return EcosystemPlan((), None, exploration_fraction, family_cap, 1.0, 0, 0)

    quarantined = {
        name for name, profile in unique.items()
        if profile.state is SpecialistState.QUARANTINED
    }
    available = {
        name: profile for name, profile in unique.items()
        if name not in quarantined
    }
    if not available:
        allocations = tuple(
            AttentionAllocation(
                specialist=name,
                family=profile.family,
                state=profile.state,
                attention_fraction=0.0,
                base_weight=0.0,
                reason="quarantined",
            )
            for name, profile in sorted(unique.items())
        )
        return EcosystemPlan(
            allocations,
            None,
            exploration_fraction,
            family_cap,
            1.0,
            0,
            len(quarantined),
        )

    shadows = {
        name: profile for name, profile in available.items()
        if profile.state is SpecialistState.SHADOW
    }
    established = {
        name: profile for name, profile in available.items()
        if profile.state is not SpecialistState.SHADOW
    }

    if shadows and established:
        exploration_pool = exploration_fraction
        exploitation_pool = 1.0 - exploration_fraction
    elif shadows:
        exploration_pool = exploration_fraction
        exploitation_pool = 0.0
    else:
        exploration_pool = 0.0
        exploitation_pool = 1.0

    shadow_weights = {
        name: max(0.01, specialist_attention_multiplier(profile))
        for name, profile in shadows.items()
    }
    established_weights = {
        name: specialist_attention_multiplier(profile)
        for name, profile in established.items()
    }

    shares = {
        **_normalize(shadow_weights, exploration_pool),
        **_normalize(established_weights, exploitation_pool),
    }

    # If all established evidence weights are zero, keep that pool idle rather than forcing
    # attention into a specialist with no support.
    families = {name: profile.family for name, profile in available.items()}
    shares = _apply_family_cap(shares, families, cap=family_cap)

    # Negative after-cost observations reduce further research regardless of the
    # competing weights. Do this after normalization so a lone losing specialist
    # cannot regain the entire pool. Released attention stays idle. These returns
    # may be paper evidence; this is a research discount, never realized dollar EV.
    for name, profile in available.items():
        if profile.after_cost_return is not None and profile.after_cost_return < 0:
            shares[name] *= 1 / (1 + abs(profile.after_cost_return) / negative_return_scale)

    allocations: list[AttentionAllocation] = []
    for name, profile in sorted(unique.items()):
        if name in quarantined:
            allocation = 0.0
            base_weight = 0.0
            reason = "quarantined"
        else:
            allocation = max(0.0, shares.get(name, 0.0))
            base_weight = specialist_attention_multiplier(profile)
            if profile.state is SpecialistState.SHADOW:
                reason = "bounded exploration"
            elif allocation > 0:
                reason = "evidence-weighted exploitation"
            else:
                reason = "no evidence-backed attention weight"
            if profile.after_cost_return is not None and profile.after_cost_return < 0:
                reason += "; reduced by negative after-cost research evidence"
        allocations.append(
            AttentionAllocation(
                specialist=name,
                family=profile.family,
                state=profile.state,
                attention_fraction=allocation,
                base_weight=base_weight,
                reason=reason,
            )
        )

    allocated = sum(item.attention_fraction for item in allocations)
    dominant = max(
        allocations,
        key=lambda item: item.attention_fraction,
        default=None,
    )
    dominant_name = (
        dominant.specialist
        if dominant is not None and dominant.attention_fraction > 0
        else None
    )

    return EcosystemPlan(
        allocations=tuple(allocations),
        dominant_specialist=dominant_name,
        exploration_fraction=exploration_fraction,
        family_cap=family_cap,
        idle_fraction=max(0.0, 1.0 - allocated),
        active_specialists=len(available),
        quarantined_specialists=len(quarantined),
    )
