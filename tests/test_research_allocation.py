from dataclasses import replace
from decimal import Decimal

import pytest

from noema.ecosystem import allocate_specialist_attention
from noema.research_allocation import CollectionQuotas, collection_quotas
from noema.specialists import SpecialistProfile, SpecialistState


def profile(name, family, state):
    return SpecialistProfile(name, family, state, 0, 0.0, None, None, None)


def plan():
    return allocate_specialist_attention([
        profile("kalshi-history", "prediction_markets", SpecialistState.PAPER),
        profile("trench-1", "solana_new_tokens", SpecialistState.SHADOW),
    ])


def quotas(allocation, cycle_id=1, **overrides):
    limits = {
        "kalshi_market_limit": 100,
        "kalshi_event_limit": 12,
        "trench_due_limit": 12,
        "trench_enrichment_limit": 1,
    }
    return collection_quotas(allocation, cycle_id=cycle_id, **(limits | overrides))


def test_collection_quotas_preserve_concentration_and_idle() -> None:
    result = quotas(plan())
    assert result == CollectionQuotas(65, 7, 1, 0)
    assert result.kalshi_markets < 100


def test_fractional_entitlement_accumulates_without_rounding_up_each_cycle() -> None:
    allocation = plan()
    totals = {field: 0 for field in CollectionQuotas.__dataclass_fields__}
    limits = {"kalshi_markets": 100, "kalshi_event_checks": 12,
              "trench_due": 12, "trench_enrichment": 1}
    shares = {"kalshi_markets": Decimal("0.65"),
              "kalshi_event_checks": Decimal("0.65"),
              "trench_due": Decimal("0.15"), "trench_enrichment": Decimal("0.15")}
    for cycle_id in range(1, 101):
        result = quotas(allocation, cycle_id)
        for field, total in totals.items():
            assert 0 <= getattr(result, field) <= limits[field]
            totals[field] = total + getattr(result, field)
            assert totals[field] == int(cycle_id * limits[field] * shares[field])
    assert totals["trench_enrichment"] == 15


def test_restart_uses_persisted_cycle_phase() -> None:
    allocation = plan()
    assert quotas(allocation, 6).trench_enrichment == 0
    assert quotas(allocation, 7).trench_enrichment == 1
    assert quotas(allocation, 7) == quotas(allocation, 7)


def test_all_quarantined_specialists_have_no_discretionary_work() -> None:
    allocation = allocate_specialist_attention([
        profile("kalshi-history", "prediction_markets", SpecialistState.QUARANTINED),
        profile("trench-1", "solana_new_tokens", SpecialistState.QUARANTINED),
    ])
    for cycle_id in range(1, 20):
        assert quotas(allocation, cycle_id) == CollectionQuotas()


def test_unavailable_domains_are_not_replaced_by_other_work() -> None:
    allocation = allocate_specialist_attention([
        profile("future-domain", "internet_native", SpecialistState.PAPER),
    ])
    assert quotas(allocation) == CollectionQuotas()
    assert quotas(None) == CollectionQuotas()


def test_zero_limits_never_schedule_work() -> None:
    assert quotas(plan(), kalshi_market_limit=0, kalshi_event_limit=0,
                  trench_due_limit=0, trench_enrichment_limit=0) == CollectionQuotas()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_attention_plan_fails_closed(value) -> None:
    allocation = plan()
    malformed = replace(allocation, allocations=(
        replace(allocation.allocations[0], attention_fraction=value),
        allocation.allocations[1],
    ))
    assert quotas(malformed) == CollectionQuotas()


def test_quarantined_share_and_broken_concentration_fail_closed() -> None:
    allocation = plan()
    malformed = replace(allocation, allocations=(
        replace(allocation.allocations[0], state=SpecialistState.QUARANTINED),
        allocation.allocations[1],
    ))
    assert quotas(malformed) == CollectionQuotas()
    assert quotas(replace(allocation, family_cap=0.1)) == CollectionQuotas()
    assert quotas(replace(allocation, idle_fraction=0.0)) == CollectionQuotas()


@pytest.mark.parametrize("value", [-1, True, float("nan"), 0.5])
def test_invalid_collection_limit_rejected(value) -> None:
    with pytest.raises(ValueError):
        quotas(plan(), kalshi_market_limit=value)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), 2**63])
def test_invalid_cycle_id_rejected(value) -> None:
    with pytest.raises(ValueError):
        quotas(plan(), cycle_id=value)
