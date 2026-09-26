from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .evaluation import score_forecast
from .history_forecaster import MODEL_VERSION


@dataclass(frozen=True)
class PairedEvaluation:
    model_version: str
    distinct_resolved_markets: int
    distinct_resolved_events: int
    candidate_brier: float | None
    market_baseline_brier: float | None
    brier_improvement: float | None
    candidate_log_loss: float | None
    market_baseline_log_loss: float | None
    status: str
    live_eligible: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def compare_history_to_market(path: str = "data/noema.db") -> PairedEvaluation:
    empty = PairedEvaluation(MODEL_VERSION, 0, 0, None, None, None, None, None, "no_pairs")
    if not Path(path).exists():
        return empty
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT f.venue, f.market_id, f.snapshot_json, f.forecast_json,
                   o.outcome_yes, o.resolved_at
            FROM forecast_ledger AS f JOIN outcomes AS o
              ON f.venue = o.venue AND f.market_id = o.market_id
            ORDER BY f.id ASC
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return empty
    finally:
        conn.close()

    baselines: dict[tuple[str, str, str], float] = {}
    candidates: dict[tuple[str, str], tuple[float, float, int]] = {}
    for venue, ticker, snapshot_json, forecast_json, outcome, resolved in rows:
        snapshot = json.loads(snapshot_json)
        forecast = json.loads(forecast_json)
        capture = _time(snapshot.get("captured_at"))
        settlement = _time(resolved)
        if capture is None or settlement is None or capture >= settlement:
            continue
        capture_key = capture.isoformat()
        key = (venue, ticker)
        model = forecast.get("model_version")
        if model == "market-baseline-v1":
            baselines.setdefault((*key, capture_key), float(forecast["probability_yes"]))
        elif model == MODEL_VERSION and key not in candidates:
            candidates[key] = (float(forecast["probability_yes"]), capture_key, int(outcome))

    paired: dict[tuple[str, str], list[tuple[float, float, int]]] = {}
    for (venue, ticker), (candidate, capture_key, result) in candidates.items():
        baseline = baselines.get((venue, ticker, capture_key))
        if baseline is not None:
            event = ticker.rsplit("-", 1)[0]
            paired.setdefault((venue, event), []).append((candidate, baseline, result))
    if not paired:
        return empty

    event_count = len(paired)
    market_count = sum(len(rows) for rows in paired.values())
    def event_mean(metric: str, baseline: bool = False) -> float:
        return sum(
            sum(
                getattr(score_forecast(b if baseline else p, y), metric)
                for p, b, y in rows
            ) / len(rows)
            for rows in paired.values()
        ) / event_count

    model_brier = event_mean("brier")
    baseline_brier = event_mean("brier", baseline=True)
    model_log = event_mean("log_loss")
    baseline_log = event_mean("log_loss", baseline=True)
    return PairedEvaluation(
        MODEL_VERSION, market_count, event_count, model_brier, baseline_brier,
        baseline_brier - model_brier, model_log, baseline_log,
        (
            "collect_more" if event_count < 100
            else "candidate_not_better"
            if model_brier >= baseline_brier or model_log >= baseline_log
            else "research_review_required"
        ),
    )
