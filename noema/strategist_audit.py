from __future__ import annotations

from dataclasses import dataclass

from .decision_quality import DecisionQuality, assess_decision_quality
from .edge import CostAssumptions, EdgeComparison, compare_binary_edges
from .research_guard import ResearchEvidence, ResearchVerdict, audit_research
from .strategist import EnsembleBelief, ModelBelief, BayesianEnsembler
from .uncertainty import UncertaintyAssessment, UncertaintyInputs, assess_uncertainty


@dataclass(frozen=True)
class StrategistAudit:
    ensemble: EnsembleBelief
    uncertainty: UncertaintyAssessment
    edges: EdgeComparison
    decision_quality: DecisionQuality
    research_verdict: ResearchVerdict | None


def build_strategist_audit(
    *,
    market_probability: float,
    yes_ask: float | None,
    no_ask: float | None,
    beliefs: list[ModelBelief],
    data_staleness_seconds: float,
    liquidity_usd: float | None,
    spread: float | None,
    fee_fraction: float = 0.0,
    slippage_fraction: float = 0.0,
    research_evidence: ResearchEvidence | None = None,
) -> StrategistAudit:
    ensemble = BayesianEnsembler().combine(
        market_probability=market_probability,
        beliefs=beliefs,
    )
    uncertainty = assess_uncertainty(
        UncertaintyInputs(
            interval_width=ensemble.upper_bound - ensemble.lower_bound,
            model_disagreement=ensemble.disagreement,
            data_staleness_seconds=data_staleness_seconds,
            liquidity_usd=liquidity_usd,
            spread=spread,
        )
    )
    edges = compare_binary_edges(
        probability_yes=ensemble.probability_yes,
        yes_ask=yes_ask,
        no_ask=no_ask,
        costs=CostAssumptions(
            fee_fraction=fee_fraction,
            slippage_fraction=slippage_fraction,
            uncertainty_haircut=uncertainty.penalty,
        ),
    )
    quality = assess_decision_quality(
        probability_yes=ensemble.probability_yes,
        market_probability=market_probability,
        lower_bound=ensemble.lower_bound,
        upper_bound=ensemble.upper_bound,
    )
    verdict = audit_research(research_evidence) if research_evidence else None
    return StrategistAudit(
        ensemble=ensemble,
        uncertainty=uncertainty,
        edges=edges,
        decision_quality=quality,
        research_verdict=verdict,
    )
