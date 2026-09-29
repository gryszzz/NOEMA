from __future__ import annotations

import math
import sqlite3

from .ecosystem import EcosystemPlan, allocate_specialist_attention
from .ecosystem_store import EcosystemStore
from .research_feedback import apply_research_feedback
from .specialists import SpecialistState


def ensure_default_specialists(db_path: str) -> EcosystemStore:
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
    return store


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

    store = ensure_default_specialists(db_path)
    plan = allocate_specialist_attention(
        store.profiles(),
        exploration_fraction=exploration_fraction,
        family_cap=family_cap,
    )
    plan = apply_research_feedback(db_path, plan)
    store.record_plan(plan)
    return plan


def record_mission_allocation_review(
    db_path: str,
    mission_id: str,
    specialist: str,
    mission_status: str,
    result: dict[str, object],
) -> dict[str, object]:
    """Recompute attention from persisted evidence and link a decision to this mission."""
    store = EcosystemStore(db_path)
    try:
        previous = store.latest_plan()
    finally:
        store.conn.close()

    plan = review_research_ecosystem(db_path)

    def share(payload: dict[str, object] | None) -> float | None:
        if not payload:
            return None
        allocations = payload.get("allocations")
        if not isinstance(allocations, list):
            return None
        for item in allocations:
            if not isinstance(item, dict) or item.get("specialist") != specialist:
                continue
            value = item.get("attention_fraction")
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and 0 <= value <= 1):
                return float(value)
        return None

    previous_share = share(previous)
    current_plan = {
        "allocations": [
            {"specialist": item.specialist, "family": item.family,
             "state": item.state.value, "attention_fraction": item.attention_fraction}
            for item in plan.allocations
        ],
    }
    current_share = share(current_plan)
    current = next((item for item in plan.allocations if item.specialist == specialist), None)
    if current is not None and current.state is SpecialistState.QUARANTINED:
        outcome = "QUARANTINE"
        basis = "The persisted specialist policy is quarantined; its attention remains zero."
    elif previous_share is not None and current_share is not None and current_share > previous_share:
        outcome = "INCREASE"
        basis = "Measured specialist evidence changed the evidence-weighted attention plan; completion alone is not a reward."
    elif previous_share is not None and current_share is not None and current_share < previous_share:
        outcome = "DECREASE"
        basis = "Measured failures, critic rejection, or data defects reduced the evidence-weighted attention plan."
    else:
        outcome = "NO_CHANGE"
        basis = (
            "No verified positive after-cost economics or measured allocation change supports more attention; "
            "completion and PASS alone do not earn an increase."
        )

    measurement: dict[str, object] = {
        "mission_status": mission_status,
        "critic_verdict": None,
        "critic_accepted": None,
        "economic_edge_proven": result.get("economic_edge_proven"),
        "realized_net_value_usd": result.get("realized_net_value_usd"),
        "mission_cash_receipt_usd": result.get("mission_cash_receipt_usd"),
        "total_labels": result.get("total_labels"),
        "walk_forward_tests": result.get("walk_forward_tests"),
        "elapsed_worker_seconds": None,
        "compute_cost_usd": None,
        "model_api_cost_usd": None,
    }
    critic = result.get("critic_review")
    if isinstance(critic, dict):
        measurement["critic_verdict"] = critic.get("verdict")
        measurement["critic_accepted"] = critic.get("result_accepted")
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT r.elapsed_seconds,r.compute_cost_usd,s.estimated_model_cost_usd "
                "FROM missions m LEFT JOIN autonomous_research_runs r ON r.id=m.run_id "
                "LEFT JOIN cognitive_sessions s ON s.session_id=m.session_id "
                "WHERE m.mission_id=?", (mission_id,),
            ).fetchone()
            if row:
                measurement["elapsed_worker_seconds"] = row[0]
                measurement["compute_cost_usd"] = row[1]
                measurement["model_api_cost_usd"] = row[2]
        except sqlite3.Error:
            pass

    review: dict[str, object] = {
        "mission_id": mission_id,
        "specialist": specialist,
        "outcome": outcome,
        "basis": basis,
        "previous_attention_fraction": previous_share,
        "current_attention_fraction": current_share,
        "measurement": measurement,
        "financial_execution": False,
    }
    store = EcosystemStore(db_path)
    try:
        review["review_id"] = store.record_mission_review(plan, review)
    finally:
        store.conn.close()
    return review
