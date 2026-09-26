from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import score_forecast


@dataclass(frozen=True)
class TrustState:
    model_name: str
    resolved: int
    log_weight: float
    cumulative_log_loss: float
    cumulative_baseline_log_loss: float

    @property
    def evidence_strength(self) -> float:
        return 1 - math.exp(-self.resolved / 100.0)

    @property
    def reliability(self) -> float:
        quality = 1 / (1 + math.exp(-self.log_weight))
        return min(1.5, quality * self.evidence_strength * 1.5)


def update_trust(
    state: TrustState,
    *,
    model_probability: float,
    market_probability: float,
    outcome_yes: int,
    learning_rate: float = 0.25,
) -> TrustState:
    model_score = score_forecast(model_probability, outcome_yes)
    market_score = score_forecast(market_probability, outcome_yes)

    resolved = state.resolved + 1
    advantage = market_score.log_loss - model_score.log_loss
    step = learning_rate / math.sqrt(resolved)
    log_weight = max(-6.0, min(6.0, state.log_weight + step * advantage))

    return TrustState(
        model_name=state.model_name,
        resolved=resolved,
        log_weight=log_weight,
        cumulative_log_loss=state.cumulative_log_loss + model_score.log_loss,
        cumulative_baseline_log_loss=(
            state.cumulative_baseline_log_loss + market_score.log_loss
        ),
    )


class AdaptiveTrustStore:
    def __init__(self, path: str = "data/noema.db") -> None:
        db = Path(path)
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_trust (
                model_name TEXT PRIMARY KEY,
                state_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def get(self, model_name: str) -> TrustState:
        row = self.conn.execute(
            "SELECT state_json FROM model_trust WHERE model_name = ?",
            (model_name,),
        ).fetchone()
        if row is None:
            return TrustState(model_name, 0, 0.0, 0.0, 0.0)
        payload = json.loads(row[0])
        return TrustState(**payload)

    def put(self, state: TrustState) -> None:
        self.conn.execute(
            """
            INSERT INTO model_trust (model_name, state_json)
            VALUES (?, ?)
            ON CONFLICT(model_name) DO UPDATE SET state_json = excluded.state_json
            """,
            (state.model_name, json.dumps(asdict(state), sort_keys=True)),
        )
        self.conn.commit()
