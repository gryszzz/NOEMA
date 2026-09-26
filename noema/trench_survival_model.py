from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .evaluation import expected_calibration_error, score_forecast

MODEL_VERSION = "trench-survival-logistic-v1"
MIN_TRAIN_LABELS = 50
MIN_TEST_LABELS = 20
ONE_HOUR_SECONDS = 3600


@dataclass(frozen=True)
class TrenchSurvivalExample:
    candidate_id: str
    captured_at: datetime
    label_observed_at: datetime
    features: dict[str, object]
    control: dict[str, object]
    survived: int


@dataclass(frozen=True)
class TrenchSurvivalAudit:
    model_version: str
    status: str
    total_labels: int
    training_labels: int
    walk_forward_tests: int
    model_brier: float | None = None
    baseline_brier: float | None = None
    brier_improvement: float | None = None
    model_log_loss: float | None = None
    baseline_log_loss: float | None = None
    calibration_error: float | None = None
    paper_forecast_eligible: bool = False
    live_eligible: bool = False
    weights: tuple[float, ...] | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TrenchPaperModel:
    model_id: str
    evidence_fingerprint: str
    training_labels: int
    baseline_probability: float
    weights: tuple[float, ...]


FEATURE_NAMES = (
    "return_fraction",
    "max_drawdown_fraction",
    "liquidity_growth_fraction",
    "signed_flow_imbalance",
    "buyer_growth_fraction",
    "buyer_growth_missing",
    "buyer_acceleration",
    "buyer_acceleration_missing",
    "participation_balance",
    "participation_missing",
    "organic_buyer_share",
    "organic_buyer_missing",
    "organic_volume_fraction",
    "organic_volume_missing",
    "top_holder_fraction",
    "top_holder_missing",
    "top5_holder_fraction",
    "top5_holder_missing",
    "holder_hhi",
    "holder_hhi_missing",
    "creator_supply_fraction",
    "creator_supply_missing",
    "organic_score",
    "organic_score_missing",
    "mint_authority",
    "mint_authority_unknown",
    "freeze_authority",
    "freeze_authority_unknown",
    "permanent_delegate",
    "permanent_delegate_unknown",
    "transfer_hook",
    "transfer_hook_unknown",
    "transfer_fee",
    "transfer_fee_unknown",
    "token_2022",
    "suspicious_flag",
)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _optional_feature(
    features: dict[str, object],
    key: str,
    *,
    scale: float = 1.0,
    low: float = 0.0,
    high: float = 1.0,
    neutral: float = 0.5,
) -> tuple[float, float]:
    raw = features.get(key)
    if raw is None:
        return neutral, 1.0
    value = float(raw) / scale
    return _clip(value, low, high), 0.0


def _tri_state(control: dict[str, object], key: str) -> tuple[float, float]:
    raw = control.get(key)
    if raw is True:
        return 1.0, 0.0
    if raw is False:
        return 0.0, 0.0
    return 0.5, 1.0


def vectorize_example(example: TrenchSurvivalExample) -> tuple[float, ...]:
    f = example.features
    c = example.control

    buyer_growth, buyer_growth_missing = _optional_feature(
        f, "buyer_growth_fraction", scale=10.0
    )
    buyer_accel, buyer_accel_missing = _optional_feature(
        f,
        "buyer_acceleration",
        low=-1.0,
        high=1.0,
        neutral=0.0,
    )
    participation, participation_missing = _optional_feature(f, "participation_balance")
    organic_buyers, organic_buyers_missing = _optional_feature(f, "organic_buyer_share")
    organic_volume, organic_volume_missing = _optional_feature(f, "organic_volume_fraction")
    top_holder, top_holder_missing = _optional_feature(f, "top_holder_fraction")
    top5, top5_missing = _optional_feature(f, "top5_holder_fraction")
    hhi, hhi_missing = _optional_feature(f, "holder_hhi")
    creator, creator_missing = _optional_feature(f, "creator_supply_fraction")
    organic_score, organic_score_missing = _optional_feature(
        f, "organic_score", scale=100.0
    )

    mint_authority, mint_unknown = _tri_state(c, "mint_authority_present")
    freeze_authority, freeze_unknown = _tri_state(c, "freeze_authority_present")
    permanent_delegate, delegate_unknown = _tri_state(c, "permanent_delegate_present")
    transfer_hook, hook_unknown = _tri_state(c, "transfer_hook_present")
    transfer_fee_raw = c.get("transfer_fee_bps")
    if transfer_fee_raw is None:
        transfer_fee, transfer_fee_unknown = 0.0, 1.0
    else:
        transfer_fee = _clip(float(transfer_fee_raw) / 1000.0, 0.0, 1.0)
        transfer_fee_unknown = 0.0

    token_program = str(c.get("token_program") or "")
    token_2022 = float("2022" in token_program.lower())

    values = (
        _clip(float(f.get("return_fraction", 0.0)) / 5.0, -0.2, 1.0),
        _clip(float(f.get("max_drawdown_fraction", 0.0)), 0.0, 1.0),
        _clip(float(f.get("liquidity_growth_fraction", 0.0)) / 5.0, -0.2, 1.0),
        _clip(float(f.get("signed_flow_imbalance", 0.0)), -1.0, 1.0),
        buyer_growth,
        buyer_growth_missing,
        buyer_accel,
        buyer_accel_missing,
        participation,
        participation_missing,
        organic_buyers,
        organic_buyers_missing,
        organic_volume,
        organic_volume_missing,
        top_holder,
        top_holder_missing,
        top5,
        top5_missing,
        hhi,
        hhi_missing,
        creator,
        creator_missing,
        organic_score,
        organic_score_missing,
        mint_authority,
        mint_unknown,
        freeze_authority,
        freeze_unknown,
        permanent_delegate,
        delegate_unknown,
        transfer_hook,
        hook_unknown,
        transfer_fee,
        transfer_fee_unknown,
        token_2022,
        float(bool(c.get("suspicious_flag"))),
    )
    if len(values) != len(FEATURE_NAMES):
        raise RuntimeError("Trench survival feature contract mismatch")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("non-finite Trench survival feature")
    return values


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-_clip(value, -30.0, 30.0)))


def fit_logistic(
    examples: list[TrenchSurvivalExample],
    *,
    l2: float = 0.08,
    learning_rate: float = 0.20,
    iterations: int = 350,
) -> tuple[float, ...]:
    if not examples:
        raise ValueError("training examples required")
    if l2 < 0 or learning_rate <= 0 or iterations <= 0:
        raise ValueError("invalid logistic training parameters")

    matrix = [vectorize_example(example) for example in examples]
    labels = [example.survived for example in examples]
    dimensions = len(FEATURE_NAMES)
    weights = [0.0] * (dimensions + 1)
    n = len(examples)

    for _ in range(iterations):
        gradient = [0.0] * (dimensions + 1)
        for row, label in zip(matrix, labels, strict=True):
            linear = weights[0] + sum(
                weights[index + 1] * value
                for index, value in enumerate(row)
            )
            error = _sigmoid(linear) - label
            gradient[0] += error
            for index, value in enumerate(row):
                gradient[index + 1] += error * value

        gradient[0] /= n
        for index in range(1, len(gradient)):
            gradient[index] = gradient[index] / n + l2 * weights[index]

        for index in range(len(weights)):
            weights[index] -= learning_rate * gradient[index]

    return tuple(weights)


def predict_survival(
    example: TrenchSurvivalExample,
    weights: tuple[float, ...],
) -> float:
    if len(weights) != len(FEATURE_NAMES) + 1:
        raise ValueError("invalid Trench survival weights")
    row = vectorize_example(example)
    linear = weights[0] + sum(
        weights[index + 1] * value
        for index, value in enumerate(row)
    )
    return _sigmoid(linear)


def _aware(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def survival_label(
    *,
    reference_liquidity_usd: float,
    final_tick: dict[str, object],
    final_control: dict[str, object],
    max_drawdown_fraction: float,
) -> int:
    final_liquidity = float(final_tick.get("liquidity_usd", 0.0))
    final_price = float(final_tick.get("price_usd", 0.0))
    liquidity_retention = (
        final_liquidity / reference_liquidity_usd
        if reference_liquidity_usd > 0
        else 0.0
    )
    return int(
        final_price > 0
        and liquidity_retention >= 0.50
        and max_drawdown_fraction < 0.70
        and not bool(final_control.get("suspicious_flag"))
    )


def load_verified_examples(path: str = "data/noema.db") -> list[TrenchSurvivalExample]:
    if not Path(path).exists():
        return []

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT c.candidate_id, c.captured_at, c.reference_liquidity_usd,
                   c.assessment_json, five.control_json,
                   one.tick_json, one.control_json, one.observed_at,
                   cf.max_drawdown_fraction
            FROM trench_candidates c
            JOIN trench_observations five
              ON five.mint = c.token_mint AND five.horizon_seconds = 300
            JOIN trench_observations one
              ON one.mint = c.token_mint AND one.horizon_seconds = ?
            JOIN trench_counterfactuals cf
              ON cf.candidate_id = c.candidate_id AND cf.horizon_seconds = ?
            ORDER BY c.captured_at, c.candidate_id
            """,
            (ONE_HOUR_SECONDS, ONE_HOUR_SECONDS),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    examples: list[TrenchSurvivalExample] = []
    for (
        candidate_id,
        captured_raw,
        reference_liquidity,
        assessment_json,
        control_json,
        final_tick_json,
        final_control_json,
        observed_raw,
        max_drawdown,
    ) in rows:
        captured = _aware(captured_raw)
        observed = _aware(observed_raw)
        if captured is None or observed is None or observed <= captured:
            continue
        try:
            assessment = json.loads(str(assessment_json))
            features = assessment["features"]
            control = json.loads(str(control_json))
            final_tick = json.loads(str(final_tick_json))
            final_control = json.loads(str(final_control_json))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not all(
            isinstance(value, dict)
            for value in (features, control, final_tick, final_control)
        ):
            continue
        label = survival_label(
            reference_liquidity_usd=float(reference_liquidity),
            final_tick=final_tick,
            final_control=final_control,
            max_drawdown_fraction=float(max_drawdown),
        )
        examples.append(
            TrenchSurvivalExample(
                candidate_id=str(candidate_id),
                captured_at=captured,
                label_observed_at=observed,
                features=features,
                control=control,
                survived=label,
            )
        )
    return examples


def audit_examples(
    examples: list[TrenchSurvivalExample],
    *,
    min_train: int = MIN_TRAIN_LABELS,
    min_test: int = MIN_TEST_LABELS,
) -> TrenchSurvivalAudit:
    if min_train <= 0 or min_test <= 0:
        raise ValueError("minimum sample sizes must be positive")

    ordered = sorted(examples, key=lambda row: (row.captured_at, row.candidate_id))
    if len(ordered) < min_train + min_test:
        return TrenchSurvivalAudit(
            MODEL_VERSION,
            "insufficient_forward_labels",
            len(ordered),
            min(len(ordered), min_train),
            max(0, len(ordered) - min_train),
        )

    scored: list[tuple[float, float, int]] = []
    for index in range(min_train, len(ordered)):
        current = ordered[index]
        training = [
            prior
            for prior in ordered[:index]
            if prior.label_observed_at < current.captured_at
        ]
        if len(training) < min_train:
            continue

        weights = fit_logistic(training)
        model_probability = predict_survival(current, weights)
        # Laplace-smoothed constant prior is the transparent baseline.
        baseline_probability = (
            sum(row.survived for row in training) + 1
        ) / (len(training) + 2)
        scored.append((model_probability, baseline_probability, current.survived))

    if len(scored) < min_test:
        return TrenchSurvivalAudit(
            MODEL_VERSION,
            "insufficient_walk_forward_tests",
            len(ordered),
            min_train,
            len(scored),
        )

    model_scores = [score_forecast(p, y) for p, _, y in scored]
    baseline_scores = [score_forecast(b, y) for _, b, y in scored]
    model_brier = sum(row.brier for row in model_scores) / len(model_scores)
    baseline_brier = sum(row.brier for row in baseline_scores) / len(baseline_scores)
    model_log = sum(row.log_loss for row in model_scores) / len(model_scores)
    baseline_log = sum(row.log_loss for row in baseline_scores) / len(baseline_scores)
    calibration = expected_calibration_error([(p, y) for p, _, y in scored])

    better = model_brier < baseline_brier and model_log < baseline_log
    final_training = [
        row
        for row in ordered
        if row.label_observed_at <= max(item.label_observed_at for item in ordered)
    ]
    final_weights = fit_logistic(final_training)

    return TrenchSurvivalAudit(
        model_version=MODEL_VERSION,
        status="research_review_required" if better else "model_not_better",
        total_labels=len(ordered),
        training_labels=len(final_training),
        walk_forward_tests=len(scored),
        model_brier=model_brier,
        baseline_brier=baseline_brier,
        brier_improvement=baseline_brier - model_brier,
        model_log_loss=model_log,
        baseline_log_loss=baseline_log,
        calibration_error=calibration,
        paper_forecast_eligible=better,
        live_eligible=False,
        weights=final_weights if better else None,
    )


def _fingerprint(examples: list[TrenchSurvivalExample]) -> str:
    payload = "\n".join(
        f"{row.candidate_id}:{row.survived}:{row.label_observed_at.isoformat()}"
        for row in examples
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class TrenchSurvivalAuditStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trench_survival_audits (
                fingerprint TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def get(self, fingerprint: str) -> TrenchSurvivalAudit | None:
        row = self.conn.execute(
            "SELECT payload_json FROM trench_survival_audits WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        raw = json.loads(str(row[0]))
        if raw.get("weights") is not None:
            raw["weights"] = tuple(float(value) for value in raw["weights"])
        return TrenchSurvivalAudit(**raw)

    def put(self, fingerprint: str, audit: TrenchSurvivalAudit) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO trench_survival_audits (
                fingerprint, created_at, payload_json
            ) VALUES (?, ?, ?)
            """,
            (
                fingerprint,
                datetime.now(UTC).isoformat(),
                json.dumps(audit.as_dict(), sort_keys=True),
            ),
        )
        self.conn.commit()


def _audit_for_examples(
    path: str,
    examples: list[TrenchSurvivalExample],
) -> tuple[str, TrenchSurvivalAudit]:
    fingerprint = _fingerprint(examples)
    store = TrenchSurvivalAuditStore(path)
    cached = store.get(fingerprint)
    if cached is not None:
        return fingerprint, cached
    audit = audit_examples(examples)
    store.put(fingerprint, audit)
    return fingerprint, audit


def audit_database(path: str = "data/noema.db") -> TrenchSurvivalAudit:
    _, audit = _audit_for_examples(path, load_verified_examples(path))
    return audit


def audit_at_cutoff(
    path: str,
    cutoff: datetime,
) -> tuple[str, TrenchSurvivalAudit, list[TrenchSurvivalExample]]:
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    cutoff = cutoff.astimezone(UTC)
    examples = [
        row
        for row in load_verified_examples(path)
        if row.captured_at < cutoff and row.label_observed_at < cutoff
    ]
    fingerprint, audit = _audit_for_examples(path, examples)
    return fingerprint, audit, examples


def paper_model_at_cutoff(
    path: str,
    cutoff: datetime,
) -> TrenchPaperModel | None:
    fingerprint, audit, examples = audit_at_cutoff(path, cutoff)
    if not audit.paper_forecast_eligible or audit.weights is None or not examples:
        return None

    baseline = (sum(row.survived for row in examples) + 1) / (len(examples) + 2)
    model_id = f"{MODEL_VERSION}:{fingerprint[:16]}"
    return TrenchPaperModel(
        model_id=model_id,
        evidence_fingerprint=fingerprint,
        training_labels=len(examples),
        baseline_probability=baseline,
        weights=audit.weights,
    )
