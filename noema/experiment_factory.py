from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .research_trials import ResearchTrialStore
from .specialist_evolution import EvolutionDecision, SpecialistEvidence
from .specialists import SpecialistProfile, SpecialistState
from .trench_survival_model import MIN_TEST_LABELS, MIN_TRAIN_LABELS


@dataclass(frozen=True)
class ExperimentProposal:
    specialist: str
    family: str
    hypothesis: str
    params: dict[str, Any]
    feature_set_version: str
    priority: float
    reason: str


@dataclass(frozen=True)
class RegisteredExperiment:
    trial_id: str
    proposal: ExperimentProposal
    status: str = "registered"


def _bounded_priority(value: float) -> float:
    return max(0.0, min(1.0, value))


def handler_for_contract(
    family: str, feature_set_version: str, params: dict[str, Any]
) -> tuple[str, str] | None:
    """Return the exact allowlisted worker for a persisted experiment contract."""

    if (
        family == "agent_services_opportunity_qualification"
        and feature_set_version == "commercial-qualification-v1"
        and params == {"experiment": "commercial_opportunity_scan", "version": "v1"}
    ):
        return "NOEMA", "commercial_opportunity_scan"
    if (
        family == "prediction_markets_data_quality"
        and feature_set_version == "market-data-v1"
        and params == {"experiment": "market_data_quality", "version": "v1"}
    ):
        return "kalshi-history", "market_data_quality"
    if (
        family == "prediction_markets_execution"
        and feature_set_version == "execution-v1"
        and params == {
            "experiment": "cost_threshold_sweep",
            "search": "predeclared_grid",
            "objective": "after_cost_return",
            "must_record_all_variants": True,
        }
    ):
        return "kalshi-history", "cost_threshold_sweep"
    if (
        family == "trench_survival"
        and feature_set_version == "trench-v1"
        and params == {
            "model": "logistic_baseline",
            "target": "survival_1h",
            "feature_set": "trench-v1",
            "validation": "purged_expanding_walk_forward",
            "calibration": "none",
        }
    ):
        return "trench-1", "trench_survival_logistic"
    return None


def propose_challengers(
    profile: SpecialistProfile,
    evidence: SpecialistEvidence,
    decision: EvolutionDecision | None,
) -> tuple[ExperimentProposal, ...]:
    """Create deterministic research challengers from measured evidence gaps.

    Proposals are experiments only. They do not alter production code, specialist state,
    wallet policy, or capital.
    """

    proposals: list[ExperimentProposal] = []
    reasons = () if decision is None else decision.reasons
    reason_text = "; ".join(reasons)

    if (
        profile.name == "trench-1"
        and evidence.resolved >= MIN_TRAIN_LABELS + MIN_TEST_LABELS
        and evidence.brier is None
    ):
        proposals.append(
            ExperimentProposal(
                specialist=profile.name,
                family="trench_survival",
                hypothesis=(
                    "Early Trench-v1 features predict one-hour token survival better "
                    "than a constant base-rate baseline."
                ),
                params={
                    "model": "logistic_baseline",
                    "target": "survival_1h",
                    "feature_set": "trench-v1",
                    "validation": "purged_expanding_walk_forward",
                    "calibration": "none",
                },
                feature_set_version="trench-v1",
                priority=0.90,
                reason="enough forward labels to establish the simplest survival baseline",
            )
        )
    if evidence.calibration_error is not None and evidence.calibration_error > 0.10:
        proposals.append(
            ExperimentProposal(
                specialist=profile.name,
                family=f"{profile.family}_calibration",
                hypothesis=(
                    "Post-hoc calibration on strictly prior folds reduces calibration "
                    "error without degrading out-of-sample scoring."
                ),
                params={
                    "experiment": "calibration_challenger",
                    "methods": ["platt", "isotonic"],
                    "fit_scope": "prior_folds_only",
                },
                feature_set_version="calibration-v1",
                priority=_bounded_priority(0.60 + evidence.calibration_error),
                reason="specialist calibration is outside the active research gate",
            )
        )

    predictive_gap = (
        evidence.brier is not None
        and evidence.market_baseline_brier is not None
        and evidence.brier >= evidence.market_baseline_brier
    )
    if predictive_gap:
        proposals.append(
            ExperimentProposal(
                specialist=profile.name,
                family=f"{profile.family}_ablation",
                hypothesis=(
                    "Removing low-value features or components improves generalization "
                    "relative to the current specialist."
                ),
                params={
                    "experiment": "feature_ablation",
                    "selection_rule": "walk_forward_only",
                    "objective": "brier_then_log_loss",
                },
                feature_set_version="ablation-v1",
                priority=0.80,
                reason="current specialist does not beat its transparent baseline",
            )
        )

    if (
        evidence.resolved >= 100
        and (evidence.after_cost_return is None or evidence.after_cost_return <= 0)
    ):
        proposals.append(
            ExperimentProposal(
                specialist=profile.name,
                family=f"{profile.family}_execution",
                hypothesis=(
                    "A stricter executable-edge threshold improves after-cost paper "
                    "returns by rejecting marginal opportunities."
                ),
                params={
                    "experiment": "cost_threshold_sweep",
                    "search": "predeclared_grid",
                    "objective": "after_cost_return",
                    "must_record_all_variants": True,
                },
                feature_set_version="execution-v1",
                priority=0.85,
                reason="predictive research must survive execution costs",
            )
        )

    if (
        evidence.probability_backtest_overfit is not None
        and evidence.probability_backtest_overfit > 0.50
    ):
        proposals.append(
            ExperimentProposal(
                specialist=profile.name,
                family=f"{profile.family}_simplification",
                hypothesis=(
                    "A lower-complexity challenger preserves signal while reducing "
                    "selection instability across combinatorial splits."
                ),
                params={
                    "experiment": "complexity_reduction",
                    "objective": "lower_pbo",
                    "parameter_budget": "half_current_search",
                },
                feature_set_version="simplification-v1",
                priority=0.95,
                reason="overfit diagnostic is above the active research threshold",
            )
        )

    if profile.state is SpecialistState.QUARANTINED and not proposals:
        proposals.append(
            ExperimentProposal(
                specialist=profile.name,
                family=f"{profile.family}_diagnostic",
                hypothesis=(
                    "A minimal replay isolates whether quarantine came from data quality, "
                    "calibration, risk, or economic execution."
                ),
                params={
                    "experiment": "quarantine_replay",
                    "scope": "diagnostic_only",
                    "state_change": False,
                },
                feature_set_version="diagnostic-v1",
                priority=0.70,
                reason=reason_text or "specialist is quarantined",
            )
        )

    # Stable ordering plus cap prevents a single review from exploding the search surface.
    proposals.sort(key=lambda item: (-item.priority, item.family, item.hypothesis))
    return tuple(proposals[:3])


def register_challengers(
    db_path: str,
    *,
    profile: SpecialistProfile,
    evidence: SpecialistEvidence,
    decision: EvolutionDecision | None,
) -> tuple[RegisteredExperiment, ...]:
    store = ResearchTrialStore(db_path)
    registered: list[RegisteredExperiment] = []
    try:
        for proposal in propose_challengers(profile, evidence, decision):
            # Preserve the measured hypothesis durably, but keep unsupported contracts
            # out of the runnable queue until their exact worker exists.
            supported = handler_for_contract(
                proposal.family, proposal.feature_set_version, proposal.params,
            ) is not None
            status = "registered" if supported else "deferred"
            trial_id = store.register(
                family=proposal.family,
                hypothesis=proposal.hypothesis,
                params=proposal.params,
                feature_set_version=proposal.feature_set_version,
                status=status,
            )
            registered.append(RegisteredExperiment(trial_id, proposal, status))
    finally:
        store.conn.close()
    return tuple(registered)


def admit_deferred_challengers(db_path: str) -> tuple[str, ...]:
    """Promote deferred hypotheses once their exact allowlisted workers exist.

    Called from the normal evolution cycle, independently of whether new evidence
    was reviewed, so implementation support arriving later does not require another
    evidence change or owner-triggered registration.
    """

    store = ResearchTrialStore(db_path)
    admitted: list[str] = []
    try:
        for trial in store.deferred():
            try:
                params = json.loads(trial.params_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if handler_for_contract(trial.family, trial.feature_set_version, params) is None:
                continue
            store.set_status(trial.trial_id, "registered")
            admitted.append(trial.trial_id)
    finally:
        store.conn.close()
    return tuple(admitted)
