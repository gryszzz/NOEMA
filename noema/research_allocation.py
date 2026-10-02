"""Turn research attention into bounded work without consuming idle capacity."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .ecosystem import EcosystemPlan
from .specialists import SpecialistState


@dataclass(frozen=True)
class CollectionQuotas:
    kalshi_markets: int = 0
    kalshi_event_checks: int = 0
    trench_due: int = 0
    trench_enrichment: int = 0


def _fraction(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def validated_research_shares(plan: EcosystemPlan | None) -> dict[str, float]:
    if plan is None:
        return {}
    if (
        not _fraction(plan.idle_fraction)
        or not _fraction(plan.family_cap)
        or plan.family_cap == 0
        or not _fraction(plan.exploration_fraction)
        or plan.exploration_fraction > 0.50
    ):
        return {}
    shares: dict[str, float] = {}
    families: dict[str, float] = {}
    shadow_fraction = 0.0
    for item in plan.allocations:
        share = item.attention_fraction
        if (
            not item.specialist.strip()
            or not item.family.strip()
            or item.specialist in shares
            or not isinstance(item.state, SpecialistState)
            or not _fraction(share)
            or (item.state is SpecialistState.QUARANTINED and share != 0)
        ):
            return {}
        shares[item.specialist] = share
        families[item.family] = families.get(item.family, 0.0) + share
        if item.state is SpecialistState.SHADOW:
            shadow_fraction += share
    if (
        abs(sum(shares.values()) + plan.idle_fraction - 1) > 1e-9
        or any(share > plan.family_cap + 1e-9 for share in families.values())
        or shadow_fraction > plan.exploration_fraction + 1e-9
    ):
        return {}
    return shares


def _scheduled(limit: int, share: float, cycle_id: int) -> int:
    rate = Decimal(limit) * Decimal(str(share))
    return int(rate * cycle_id) - int(rate * (cycle_id - 1))


def collection_quotas(
    plan: EcosystemPlan | None,
    *,
    kalshi_market_limit: int,
    kalshi_event_limit: int,
    trench_due_limit: int,
    trench_enrichment_limit: int,
    cycle_id: int = 1,
) -> CollectionQuotas:
    """Schedule discretionary work from each specialist's existing attention share.

    A stable plan grants at most floor(cycles * limit * share) discretionary work
    cumulatively. Fractions carry through the monotonic persisted cycle id, so an
    enrichment limit of one can support occasional shadow research without rounding
    up every cycle. A single bounded Kalshi event-membership probe is reserved for
    an enabled, non-quarantined history specialist as observational data qualification.
    Unknown specialists, unavailable domains and idle capacity are never redistributed.
    Missing or invalid plans grant no discretionary work.

    Operational perception and already-scheduled outcome observations are separate
    obligations. In particular, trench_due bounds new discovery/sampling; the runtime
    must keep completing prior forward samples with its bounded due-observation limit
    even when new work is zero, to avoid hiding unfavorable outcomes after quarantine.
    Enrichment is independently bounded and can accompany these existing obligations.
    """
    for value in (
        kalshi_market_limit, kalshi_event_limit, trench_due_limit, trench_enrichment_limit,
    ):
        if type(value) is not int or value < 0:
            raise ValueError("collection limits must be non-negative integers")
    if trench_enrichment_limit > trench_due_limit:
        raise ValueError("enrichment limit must fit within the due-observation limit")
    if type(cycle_id) is not int or not 1 <= cycle_id <= 2**63 - 1:
        raise ValueError("cycle_id must be a positive persistent 64-bit integer")
    shares = validated_research_shares(plan)
    kalshi = shares.get("kalshi-history", 0.0)
    trench = shares.get("trench-1", 0.0)
    # Keep the public evidence pipeline observable even when adaptive attention
    # assigns no discretionary share. This single event-membership probe is
    # read-only data qualification, not a forecast, cognition call, or trade.
    # Quarantined/unknown specialists and zero configured limits still fail closed.
    kalshi_history_enabled = any(
        item.specialist == "kalshi-history"
        and item.state is not SpecialistState.QUARANTINED
        for item in plan.allocations
    ) if plan is not None and "kalshi-history" in shares else False
    kalshi_event_checks = _scheduled(kalshi_event_limit, kalshi, cycle_id)
    if kalshi_history_enabled and kalshi_event_limit > 0:
        kalshi_event_checks = max(1, kalshi_event_checks)
    return CollectionQuotas(
        kalshi_markets=_scheduled(kalshi_market_limit, kalshi, cycle_id),
        kalshi_event_checks=kalshi_event_checks,
        trench_due=_scheduled(trench_due_limit, trench, cycle_id),
        trench_enrichment=_scheduled(trench_enrichment_limit, trench, cycle_id),
    )
