from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .backtest_overfit import probabilistic_sharpe_ratio
from .evaluation import expected_calibration_error
from .history_forecaster import MODEL_VERSION
from .paired_evaluation import compare_history_to_market
from .paper_performance import settled_paper_performance
from .specialist_evolution import SpecialistEvidence
from .trench_survival_model import audit_database as audit_trench_survival


def _candidate_calibration(path: str) -> float | None:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT f.venue, f.market_id, f.forecast_json, f.snapshot_json,
                   o.outcome_yes, o.resolved_at
            FROM forecast_ledger f
            JOIN outcomes o
              ON o.venue = f.venue AND o.market_id = f.market_id
            WHERE json_extract(f.forecast_json, '$.model_version') = ?
            ORDER BY f.id
            """,
            (MODEL_VERSION,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    seen: set[tuple[str, str]] = set()
    scored: list[tuple[float, int]] = []
    for venue, market_id, forecast_json, snapshot_json, outcome, resolved_at in rows:
        key = (str(venue), str(market_id))
        if key in seen or not resolved_at:
            continue
        try:
            forecast = json.loads(str(forecast_json))
            snapshot = json.loads(str(snapshot_json))
            captured = datetime.fromisoformat(str(snapshot["captured_at"]))
            resolved = datetime.fromisoformat(str(resolved_at))
            probability = float(forecast["probability_yes"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if (
            captured.tzinfo is None
            or resolved.tzinfo is None
            or captured >= resolved
            or not 0 <= probability <= 1
        ):
            continue
        seen.add(key)
        scored.append((probability, int(outcome)))

    return expected_calibration_error(scored) if scored else None


def _paper_returns(path: str) -> list[float]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT q.quote_json, o.outcome_yes, q.quoted_at,
                   o.resolved_at, o.first_seen_at
            FROM paper_quotes q
            JOIN outcomes o
              ON o.venue = q.venue AND o.market_id = q.market_id
            WHERE q.selected = 1
            ORDER BY q.quoted_at, q.id
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    returns: list[float] = []
    for quote_json, outcome, quoted_at, resolved_at, first_seen_at in rows:
        try:
            quote = json.loads(str(quote_json))
            debit = float(quote["total_debit_usd"])
            contracts = float(quote["contracts"])
            quoted = datetime.fromisoformat(str(quoted_at))
            resolved = datetime.fromisoformat(str(resolved_at))
            first_seen = datetime.fromisoformat(str(first_seen_at))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if (
            debit <= 0
            or quoted.tzinfo is None
            or resolved.tzinfo is None
            or first_seen.tzinfo is None
            or resolved <= quoted
            or first_seen <= quoted
        ):
            continue
        net = int(outcome) * contracts - debit
        returns.append(net / debit)
    return returns


def kalshi_history_evidence(path: str = "data/noema.db") -> SpecialistEvidence:
    if not Path(path).exists():
        return SpecialistEvidence(0, None, None, None, None, None, None)

    paired = compare_history_to_market(path)
    calibration = _candidate_calibration(path)
    paper = settled_paper_performance(path)
    psr = (
        probabilistic_sharpe_ratio(paper.per_trade_returns).probabilistic_sharpe
        if len(paper.per_trade_returns) >= 3
        else None
    )

    if paired.distinct_resolved_events < 100:
        credible = None
    else:
        credible = paired.status == "research_review_required"

    return SpecialistEvidence(
        resolved=paired.distinct_resolved_events,
        brier=paired.candidate_brier,
        market_baseline_brier=paired.market_baseline_brier,
        after_cost_return=paper.after_cost_return,
        max_drawdown_fraction=paper.max_drawdown_fraction if paper.observations else None,
        calibration_error=calibration,
        research_credible=credible,
        probability_backtest_overfit=None,
        probabilistic_sharpe=psr,
    )


def trench1_evidence(
    path: str = "data/noema.db",
    *,
    horizon_seconds: int = 3600,
) -> SpecialistEvidence:
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    if not Path(path).exists():
        return SpecialistEvidence(0, None, None, None, None, None, None)

    if horizon_seconds == 3600:
        audit = audit_trench_survival(path)
        method_credible = (
            True
            if audit.status in {"research_review_required", "model_not_better"}
            else None
        )
        return SpecialistEvidence(
            resolved=audit.total_labels,
            brier=audit.model_brier,
            market_baseline_brier=audit.baseline_brier,
            after_cost_return=None,
            max_drawdown_fraction=None,
            calibration_error=audit.calibration_error,
            research_credible=method_credible,
            probability_backtest_overfit=None,
            probabilistic_sharpe=None,
        )

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT candidate_id)
            FROM trench_counterfactuals
            WHERE horizon_seconds = ?
            """,
            (horizon_seconds,),
        ).fetchone()
    except sqlite3.OperationalError:
        resolved = 0
    else:
        resolved = 0 if row is None else int(row[0])
    finally:
        conn.close()

    return SpecialistEvidence(
        resolved=resolved,
        brier=None,
        market_baseline_brier=None,
        after_cost_return=None,
        max_drawdown_fraction=None,
        calibration_error=None,
        research_credible=None,
        probability_backtest_overfit=None,
        probabilistic_sharpe=None,
    )
