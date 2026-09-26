from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .research_trials import ResearchTrialStore
from .specialist_evolution import EvolutionDecision, SpecialistEvidence
from .specialists import SpecialistProfile, SpecialistState


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


def _bounded_priority(value: float) -> float:
    return max(0.0, min(1.0, value))


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

    if profile.name == "trench-1" and evidence.resolved >= 30 and evidence.brier is None:
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
        proposals.append(
            ExperimentProposal(
                specialist=profile.name,
                family="trench_survival",
                hypothesis=(
                    "A shallow nonlinear tree challenger improves one-hour survival "
                    "classification without excessive search complexity."
                ),
                params={
                    "model": "shallow_tree_challenger",
                    "target": "survival_1h",
                    "feature_set": "trench-v1",
                    "max_depth": 3,
                    "validation": "purged_expanding_walk_forward",
                },
                feature_set_version="trench-v1",
                priority=0.75,
                reason="champion-challenger comparison after a transparent baseline",
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
    for proposal in propose_challengers(profile, evidence, decision):
        trial_id = store.register(
            family=proposal.family,
            hypothesis=proposal.hypothesis,
            params=proposal.params,
            feature_set_version=proposal.feature_set_version,
        )
        registered.append(RegisteredExperiment(trial_id, proposal))
    return tuple(registered)
