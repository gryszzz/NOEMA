from __future__ import annotations

from .ecosystem import EcosystemPlan, allocate_specialist_attention
from .ecosystem_store import EcosystemStore
from .specialists import SpecialistState


def review_research_ecosystem(
    db_path: str,
    *,
    exploration_fraction: float = 0.15,
    family_cap: float = 0.65,
) -> EcosystemPlan:
    """Build and persist one research-attention review.

    Defaults describe operational maturity only:
    - Kalshi already has a paper research loop.
    - Trench-1 is a new shadow specialist until forward evidence promotes it.

    Existing registry state is never overwritten by this bootstrap.
    """

    store = EcosystemStore(db_path)
    store.ensure_specialist(
        name="kalshi-history",
        family="prediction_markets",
        state=SpecialistState.PAPER,
    )
    store.ensure_specialist(
        name="trench-1",
        family="solana_new_tokens",
        state=SpecialistState.SHADOW,
    )
    plan = allocate_specialist_attention(
        store.profiles(),
        exploration_fraction=exploration_fraction,
        family_cap=family_cap,
    )
    store.record_plan(plan)
    return plan
