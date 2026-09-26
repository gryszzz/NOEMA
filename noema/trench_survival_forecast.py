from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .evaluation import expected_calibration_error, score_forecast
from .trench_survival_model import (
    TrenchSurvivalExample,
    paper_model_at_cutoff,
    predict_survival,
    survival_label,
)

MAX_FORWARD_RECORDING_DELAY_SECONDS = 120.0
MIN_FORWARD_RESOLUTIONS = 30


@dataclass(frozen=True)
class ForwardForecastResult:
    status: str
    candidate_id: str
    forecast_id: str | None = None
    probability_survival: float | None = None
    model_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class TrenchForwardAudit:
    status: str
    resolved_forecasts: int
    model_brier: float | None = None
    baseline_brier: float | None = None
    model_log_loss: float | None = None
    baseline_log_loss: float | None = None
    calibration_error: float | None = None
    distinct_models: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class TrenchSurvivalForecastStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trench_survival_forecasts (
                forecast_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL UNIQUE,
                token_mint TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                model_id TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                training_labels INTEGER NOT NULL,
                probability_survival REAL NOT NULL,
                baseline_probability REAL NOT NULL,
                forecast_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def exists(self, candidate_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM trench_survival_forecasts WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone() is not None

    def append(
        self,
        *,
        candidate_id: str,
        token_mint: str,
        captured_at: datetime,
        recorded_at: datetime,
        model_id: str,
        evidence_fingerprint: str,
        training_labels: int,
        probability_survival: float,
        baseline_probability: float,
        forecast_payload: dict[str, object],
    ) -> str:
        if captured_at.tzinfo is None or recorded_at.tzinfo is None:
            raise ValueError("forecast timestamps must be timezone-aware")
        if not 0 <= probability_survival <= 1 or not 0 <= baseline_probability <= 1:
            raise ValueError("forecast probabilities must be in [0, 1]")
        payload = (
            f"{candidate_id}\n{captured_at.astimezone(UTC).isoformat()}\n"
            f"{model_id}\n{evidence_fingerprint}"
        )
        forecast_id = hashlib.sha256(payload.encode()).hexdigest()[:24]
        self.conn.execute(
            """
            INSERT OR IGNORE INTO trench_survival_forecasts (
                forecast_id, candidate_id, token_mint, captured_at, recorded_at,
                model_id, evidence_fingerprint, training_labels,
                probability_survival, baseline_probability, forecast_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                forecast_id,
                candidate_id,
                token_mint,
                captured_at.astimezone(UTC).isoformat(),
                recorded_at.astimezone(UTC).isoformat(),
                model_id,
                evidence_fingerprint,
                training_labels,
                probability_survival,
                baseline_probability,
                json.dumps(forecast_payload, sort_keys=True, default=str),
            ),
        )
        self.conn.commit()
        return forecast_id


def _aware(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def record_candidate_survival_forecast(
    path: str,
    *,
    candidate_id: str,
    recorded_at: datetime | None = None,
    max_delay_seconds: float = MAX_FORWARD_RECORDING_DELAY_SECONDS,
) -> ForwardForecastResult:
    if max_delay_seconds <= 0:
        raise ValueError("max_delay_seconds must be positive")
    store = TrenchSurvivalForecastStore(path)
    if store.exists(candidate_id):
        return ForwardForecastResult("duplicate", candidate_id, detail="forecast already recorded")

    row = store.conn.execute(
        """
        SELECT c.token_mint, c.captured_at, c.assessment_json, five.control_json
        FROM trench_candidates c
        JOIN trench_observations five
          ON five.mint = c.token_mint AND five.horizon_seconds = 300
        WHERE c.candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        return ForwardForecastResult("unavailable", candidate_id, detail="candidate context missing")

    token_mint, captured_raw, assessment_json, control_json = row
    captured = _aware(captured_raw)
    if captured is None:
        return ForwardForecastResult("invalid", candidate_id, detail="candidate timestamp invalid")

    recorded_at = recorded_at or datetime.now(UTC)
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    recorded_at = recorded_at.astimezone(UTC)
    delay = (recorded_at - captured).total_seconds()
    if delay < 0 or delay > max_delay_seconds:
        return ForwardForecastResult(
            "stale",
            candidate_id,
            detail="forecast was not recorded inside the forward-evidence window",
        )

    model = paper_model_at_cutoff(path, captured)
    if model is None:
        return ForwardForecastResult(
            "model_not_eligible",
            candidate_id,
            detail="no prior Survival-v1 model has earned paper forecast eligibility",
        )

    try:
        assessment = json.loads(str(assessment_json))
        features = assessment["features"]
        control = json.loads(str(control_json))
    except (json.JSONDecodeError, KeyError, TypeError):
        return ForwardForecastResult("invalid", candidate_id, detail="candidate features invalid")
    if not isinstance(features, dict) or not isinstance(control, dict):
        return ForwardForecastResult("invalid", candidate_id, detail="candidate features invalid")

    current = TrenchSurvivalExample(
        candidate_id=candidate_id,
        captured_at=captured,
        label_observed_at=captured + timedelta(hours=1),
        features=features,
        control=control,
        survived=0,
    )
    probability = predict_survival(current, model.weights)
    payload = {
        "candidate_id": candidate_id,
        "token_mint": str(token_mint),
        "captured_at": captured.isoformat(),
        "recorded_at": recorded_at.isoformat(),
        "model_id": model.model_id,
        "evidence_fingerprint": model.evidence_fingerprint,
        "training_labels": model.training_labels,
        "probability_survival": probability,
        "baseline_probability": model.baseline_probability,
    }
    forecast_id = store.append(
        candidate_id=candidate_id,
        token_mint=str(token_mint),
        captured_at=captured,
        recorded_at=recorded_at,
        model_id=model.model_id,
        evidence_fingerprint=model.evidence_fingerprint,
        training_labels=model.training_labels,
        probability_survival=probability,
        baseline_probability=model.baseline_probability,
        forecast_payload=payload,
    )
    return ForwardForecastResult(
        "recorded",
        candidate_id,
        forecast_id=forecast_id,
        probability_survival=probability,
        model_id=model.model_id,
    )


def audit_forward_forecasts(
    path: str = "data/noema.db",
    *,
    minimum_resolutions: int = MIN_FORWARD_RESOLUTIONS,
    max_delay_seconds: float = MAX_FORWARD_RECORDING_DELAY_SECONDS,
) -> TrenchForwardAudit:
    if minimum_resolutions <= 0 or max_delay_seconds <= 0:
        raise ValueError("audit thresholds must be positive")
    if not Path(path).exists():
        return TrenchForwardAudit("no_forward_forecasts", 0)

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT f.captured_at, f.recorded_at, f.model_id,
                   f.probability_survival, f.baseline_probability,
                   c.reference_liquidity_usd,
                   one.tick_json, one.control_json, one.observed_at,
                   cf.max_drawdown_fraction
            FROM trench_survival_forecasts f
            JOIN trench_candidates c ON c.candidate_id = f.candidate_id
            JOIN trench_observations one
              ON one.mint = c.token_mint AND one.horizon_seconds = 3600
            JOIN trench_counterfactuals cf
              ON cf.candidate_id = c.candidate_id AND cf.horizon_seconds = 3600
            ORDER BY f.captured_at, f.forecast_id
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return TrenchForwardAudit("no_forward_forecasts", 0)
    finally:
        conn.close()

    scored: list[tuple[float, float, int, str]] = []
    for (
        captured_raw,
        recorded_raw,
        model_id,
        probability,
        baseline,
        reference_liquidity,
        tick_json,
        control_json,
        observed_raw,
        max_drawdown,
    ) in rows:
        captured = _aware(captured_raw)
        recorded = _aware(recorded_raw)
        observed = _aware(observed_raw)
        if captured is None or recorded is None or observed is None:
            continue
        delay = (recorded - captured).total_seconds()
        if delay < 0 or delay > max_delay_seconds or recorded >= observed:
            continue
        try:
            final_tick = json.loads(str(tick_json))
            final_control = json.loads(str(control_json))
        except json.JSONDecodeError:
            continue
        if not isinstance(final_tick, dict) or not isinstance(final_control, dict):
            continue
        outcome = survival_label(
            reference_liquidity_usd=float(reference_liquidity),
            final_tick=final_tick,
            final_control=final_control,
            max_drawdown_fraction=float(max_drawdown),
        )
        scored.append((float(probability), float(baseline), outcome, str(model_id)))

    if not scored:
        return TrenchForwardAudit("no_resolved_forward_forecasts", 0)

    model_scores = [score_forecast(p, y) for p, _, y, _ in scored]
    baseline_scores = [score_forecast(b, y) for _, b, y, _ in scored]
    model_brier = sum(row.brier for row in model_scores) / len(model_scores)
    baseline_brier = sum(row.brier for row in baseline_scores) / len(baseline_scores)
    model_log = sum(row.log_loss for row in model_scores) / len(model_scores)
    baseline_log = sum(row.log_loss for row in baseline_scores) / len(baseline_scores)
    calibration = expected_calibration_error([(p, y) for p, _, y, _ in scored])

    if len(scored) < minimum_resolutions:
        status = "insufficient_forward_resolutions"
    elif model_brier < baseline_brier and model_log < baseline_log:
        status = "research_review_required"
    else:
        status = "forward_model_not_better"

    return TrenchForwardAudit(
        status=status,
        resolved_forecasts=len(scored),
        model_brier=model_brier,
        baseline_brier=baseline_brier,
        model_log_loss=model_log,
        baseline_log_loss=baseline_log,
        calibration_error=calibration,
        distinct_models=len({model_id for _, _, _, model_id in scored}),
    )
