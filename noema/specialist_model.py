"""Paper-only learned market/history blend with chronological, event-level audit.

It learns from both the observed market quote and the independent historical
candidate. It must beat the quote on later events before review for deployment.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .evaluation import score_forecast
from .market_probability import snapshot_midpoint

MODEL_VERSION = "noema-calibration-v2"
MIN_TRAIN_EVENTS = 100
MIN_TEST_EVENTS = 30


@dataclass(frozen=True)
class Example:
    venue: str
    series: str
    event: str
    ticker: str
    captured_at: datetime
    settled_at: datetime
    first_seen_at: datetime
    price: float
    outcome: int
    history_probability: float = 0.5


@dataclass(frozen=True)
class ModelAudit:
    model_version: str
    status: str
    training_events: int
    test_events: int
    model_brier: float | None = None
    market_brier: float | None = None
    model_log_loss: float | None = None
    market_log_loss: float | None = None
    deployment_eligible: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _logit(price: float) -> float:
    p = max(0.01, min(0.99, price))
    return math.log(p / (1 - p))


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, value))))


def fit_calibration(examples: list[Example]) -> tuple[float, float, float]:
    """Regularized logistic blend; shrink toward the unmodified quote."""
    if not examples:
        raise ValueError("training examples required")
    counts: dict[tuple[str, str], int] = {}
    for row in examples:
        key = (row.venue, row.event)
        counts[key] = counts.get(key, 0) + 1
    bias, slope, history_slope = 0.0, 1.0, 0.0
    total_events = len(counts)
    for _ in range(350):
        db, ds, dh = 0.0, 0.0, 0.0
        for row in examples:
            x = _logit(row.price)
            historical = _logit(row.history_probability)
            weight = 1 / counts[(row.venue, row.event)]
            error = _sigmoid(bias + slope * x + history_slope * historical) - row.outcome
            db += weight * error
            ds += weight * error * x
            dh += weight * error * historical
        # A small prior limits overfitting and returns the market quote when
        # the learned adjustment is unsupported.
        db = db / total_events + 0.08 * bias
        ds = ds / total_events + 0.08 * (slope - 1)
        dh = dh / total_events + 0.08 * history_slope
        bias -= 0.12 * db
        slope -= 0.12 * ds
        history_slope -= 0.12 * dh
        slope = max(0.0, min(3.0, slope))
        history_slope = max(-2.0, min(2.0, history_slope))
    return bias, slope, history_slope


def predict_calibration(
    price: float, weights: tuple[float, float, float], history_probability: float,
) -> float:
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in (price, history_probability)):
        raise ValueError("model inputs must be probabilities")
    bias, slope, history_slope = weights
    return _sigmoid(
        bias + slope * _logit(price) + history_slope * _logit(history_probability)
    )


def load_verified_examples(path: str) -> list[Example]:
    """Earliest matching snapshot per market, with verified event membership."""
    if not Path(path).is_file():
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT f.venue, f.market_id, f.snapshot_json, f.forecast_json,
                   o.outcome_yes, o.resolved_at, o.first_seen_at, o.raw_json
            FROM forecast_ledger f JOIN outcomes o
              ON f.venue = o.venue AND f.market_id = o.market_id
            ORDER BY f.id
            """
        ).fetchall()
        expected = conn.execute(
            """
            SELECT venue, market_id FROM forecast_ledger
            WHERE json_extract(forecast_json, '$.model_version') = 'series-frequency-v1'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    verified: dict[tuple[str, str, str], float] = {}
    baselines: dict[tuple[str, str, str], Example] = {}
    for venue, ticker, snapshot_json, forecast_json, outcome, settled, seen, raw_json in rows:
        try:
            snapshot = json.loads(snapshot_json)
            forecast = json.loads(forecast_json)
            raw = json.loads(raw_json)
            captured = _parse_time(snapshot.get("captured_at"))
            resolved = _parse_time(settled)
            first_seen = _parse_time(seen)
            price = float(forecast.get("probability_yes"))
        except (ValueError, TypeError, AttributeError):
            continue
        if not all(isinstance(value, dict) for value in (snapshot, forecast, raw)):
            continue
        event = raw.get("event_ticker")
        if (
            not captured or not resolved or not first_seen
            or captured >= resolved or captured >= first_seen
            or not isinstance(event, str) or not ticker.startswith(event + "-")
            or venue not in {"kalshi:demo", "kalshi:production"}
            or not math.isfinite(price) or not 0 <= price <= 1
        ):
            continue
        key = (venue, ticker, captured.isoformat())
        model = forecast.get("model_version")
        if model == "series-frequency-v1":
            verified.setdefault(key, price)
        elif model == "market-baseline-v1":
            midpoint = snapshot_midpoint(snapshot)
            if midpoint is None:
                continue
            baselines.setdefault(
                key, Example(venue, event.split("-", 1)[0], event, ticker,
                             captured, resolved, first_seen, midpoint, int(outcome)),
            )
    expected_by_event: dict[tuple[str, str], set[str]] = {}
    for venue, ticker in expected:
        expected_by_event.setdefault((venue, ticker.rsplit("-", 1)[0]), set()).add(ticker)
    eligible = [
        replace(baselines[key], history_probability=historical)
        for key, historical in verified.items() if key in baselines
    ]
    observed_by_event: dict[tuple[str, str], set[str]] = {}
    for row in eligible:
        observed_by_event.setdefault((row.venue, row.event), set()).add(row.ticker)
    return [
        row for row in eligible
        if observed_by_event[(row.venue, row.event)] == expected_by_event[(row.venue, row.event)]
    ]


def audit_series(
    examples: list[Example], *, min_train: int = MIN_TRAIN_EVENTS,
    min_test: int = MIN_TEST_EVENTS,
) -> ModelAudit:
    """Walk-forward score: each test forecast trains on outcomes seen before it."""
    if min_train < 1 or min_test < 1:
        raise ValueError("minimum event counts must be positive")
    grouped: dict[tuple[str, str], list[Example]] = {}
    for row in examples:
        grouped.setdefault((row.venue, row.event), []).append(row)
    # Only a homogeneous venue/series can be evaluated in one audit.
    families = {(r.venue, r.series) for r in examples}
    if len(families) > 1:
        raise ValueError("audit one venue and series at a time")
    events = sorted(
        grouped.values(), key=lambda rows: (min(r.captured_at for r in rows), rows[0].event)
    )
    if len(events) < min_train + min_test:
        return ModelAudit(MODEL_VERSION, "insufficient_resolved_events", max(0, len(events) - min_test),
                          max(0, len(events) - min_train))

    scores: list[tuple[float, float, float, float]] = []
    for group in events[min_train:]:
        cutoff = min(row.captured_at for row in group)
        training = [
            row for prior in events for row in prior
            if row.event != group[0].event
            and row.captured_at < cutoff
            and row.settled_at < cutoff
            and row.first_seen_at < cutoff
        ]
        if len({(r.venue, r.event) for r in training}) < min_train:
            continue
        weights = fit_calibration(training)
        metrics = [
            (score_forecast(predict_calibration(row.price, weights, row.history_probability),
                            row.outcome),
             score_forecast(row.price, row.outcome))
            for row in group
        ]
        n = len(metrics)
        scores.append((
            sum(a.brier for a, _ in metrics) / n,
            sum(b.brier for _, b in metrics) / n,
            sum(a.log_loss for a, _ in metrics) / n,
            sum(b.log_loss for _, b in metrics) / n,
        ))
    if len(scores) < min_test:
        return ModelAudit(MODEL_VERSION, "insufficient_walk_forward_tests", min_train,
                          len(scores))
    means = tuple(sum(row[i] for row in scores) / len(scores) for i in range(4))
    better = means[0] < means[1] and means[2] < means[3]
    # A successful audit starts research review; it cannot unlock live money.
    return ModelAudit(MODEL_VERSION, "research_review_required" if better else
                      "model_not_better", min_train, len(scores), *means)


def audit_database(path: str) -> dict[str, dict[str, object]]:
    families: dict[tuple[str, str], list[Example]] = {}
    for row in load_verified_examples(path):
        families.setdefault((row.venue, row.series), []).append(row)
    if not families:
        return {"status": {"model_version": MODEL_VERSION, "status": "no_verified_resolved_examples"}}
    return {
        f"{venue}/{series}": audit_series(rows).as_dict()
        for (venue, series), rows in sorted(families.items())
    }
