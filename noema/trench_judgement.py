"""Optional typed model judgement for the final paper-desk pick.

The model only chooses among candidates that already passed deterministic entry rules.
It returns a typed yes/no with a probability per candidate. If cognition is disabled,
unpriced, over budget, or fails, the rules-only ranking is used instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .cognition_policy import CognitionPolicy
from .openai_client import OpenAICognitionClient, _completed_text
from .openai_config import OpenAIConfig

JUDGEMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["judgements"],
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mint", "trade", "probability_up_1h"],
                "properties": {
                    "mint": {"type": "string"},
                    "trade": {"type": "boolean"},
                    "probability_up_1h": {"type": "number"},
                },
            },
        }
    },
}

MAX_OUTPUT_TOKENS = 300


@dataclass(frozen=True)
class Judgement:
    mint: str
    trade: bool
    probability: float
    source: str  # "model" or "rules"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trench_desk_judgements (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, source TEXT NOT NULL,
            status TEXT NOT NULL, estimated_usd REAL NOT NULL DEFAULT 0,
            candidates_json TEXT NOT NULL, result_json TEXT NOT NULL
        )"""
    )


def spent_today(conn: sqlite3.Connection) -> float:
    _ensure_table(conn)
    day = datetime.now(UTC).date().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(estimated_usd), 0) FROM trench_desk_judgements WHERE ts >= ?",
        (day,),
    ).fetchone()
    return float(row[0] or 0)


def rules_ranking(candidates: list[dict[str, Any]]) -> list[Judgement]:
    """Fallback: prefer deeper liquidity, lower concentration, calmer recent move."""

    def score(c: dict[str, Any]) -> float:
        liq = min(1.0, (c.get("liquidity_usd") or 0) / 200_000.0)
        conc = 1.0 - (c.get("top10_holder_fraction") or 0.6)
        calm = 1.0 - min(1.0, abs(c.get("price_change_m5") or 0) / 15.0)
        return 0.4 * liq + 0.4 * conc + 0.2 * calm

    ranked = sorted(candidates, key=score, reverse=True)
    return [Judgement(c["mint"], True, round(score(c), 4), "rules") for c in ranked]


async def judge_candidates(
    db_path: str, candidates: list[dict[str, Any]], *,
    client: OpenAICognitionClient | None = None,
) -> tuple[list[Judgement], dict[str, Any]]:
    if not candidates:
        return [], {"status": "no_candidates"}
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _ensure_table(conn)
        status: dict[str, Any]
        enabled = os.getenv("NOEMA_DESK_JUDGEMENT_ENABLED", "0") == "1"
        budget = float(os.getenv("NOEMA_DESK_JUDGEMENT_MAX_USD_PER_DAY", "0.50"))
        if not enabled:
            status = {"status": "rules_only", "reason": "judgement disabled"}
        else:
            config = OpenAIConfig.from_env()
            try:
                policy = CognitionPolicy.from_env(provider="openai", model=config.model)
                prompt = json.dumps(candidates, sort_keys=True, default=str)
                estimate = policy.estimated_max_call_usd(
                    input_bytes=len(prompt.encode()) + 1200, max_output_tokens=MAX_OUTPUT_TOKENS)
            except ValueError:
                estimate = None
            if not config.ready or estimate is None:
                status = {"status": "rules_only", "reason": "cognition not configured or unpriced"}
            elif spent_today(conn) + estimate > min(budget, policy.max_estimated_usd_per_day):
                status = {"status": "rules_only", "reason": "daily judgement budget exhausted"}
            else:
                try:
                    judgements = await _model_judgement(
                        config, candidates, prompt, client=client, db_path=db_path)
                    conn.execute(
                        """INSERT INTO trench_desk_judgements (ts, source, status, estimated_usd,
                           candidates_json, result_json) VALUES (?,?,?,?,?,?)""",
                        (datetime.now(UTC).isoformat(), "model", "completed", estimate,
                         prompt, json.dumps([j.__dict__ for j in judgements])))
                    conn.commit()
                    return judgements, {"status": "model", "estimated_usd": estimate}
                except Exception as exc:  # noqa: BLE001 - any model failure falls back
                    status = {"status": "rules_only", "reason": f"model failed: {type(exc).__name__}"}
                    conn.execute(
                        """INSERT INTO trench_desk_judgements (ts, source, status, estimated_usd,
                           candidates_json, result_json) VALUES (?,?,?,?,?,?)""",
                        (datetime.now(UTC).isoformat(), "model", "failed", estimate, prompt,
                         json.dumps(status)))
        ranked = rules_ranking(candidates)
        conn.execute(
            """INSERT INTO trench_desk_judgements (ts, source, status, estimated_usd,
               candidates_json, result_json) VALUES (?,?,?,0,?,?)""",
            (datetime.now(UTC).isoformat(), "rules", status["status"],
             json.dumps(candidates, default=str), json.dumps([j.__dict__ for j in ranked])))
        conn.commit()
        return ranked, status
    finally:
        conn.close()


async def _model_judgement(
    config: OpenAIConfig, candidates: list[dict[str, Any]], prompt: str, *,
    client: OpenAICognitionClient | None, db_path: str,
) -> list[Judgement]:
    owns = client is None
    client = client or OpenAICognitionClient(config, usage_db_path=db_path,
                                             subsystem="trench-desks")
    body: dict[str, Any] = {
        "model": config.model,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "instructions": (
            "You are a paper-trading research judge for Solana memecoin pools. Every candidate "
            "already passed deterministic safety rules. For each mint, answer whether a short "
            "paper position is worth taking now and give the probability the price is higher "
            "in one hour. Use only the numeric fields provided. Social chatter is not provided "
            "and must not be assumed."
        ),
        "input": prompt,
        "text": {"format": {"type": "json_schema", "name": "desk_judgement",
                            "schema": JUDGEMENT_SCHEMA, "strict": True}},
    }
    try:
        payload = await client.structured_research(
            body, trace_metadata={"specialist": "trench-desks"})
    finally:
        if owns and hasattr(client, "close"):
            await client.close()
    parsed = json.loads(_completed_text(payload))
    allowed = {c["mint"] for c in candidates}
    out: list[Judgement] = []
    for item in parsed.get("judgements", []):
        mint = item.get("mint")
        probability = float(item.get("probability_up_1h", 0))
        if mint in allowed and 0 <= probability <= 1:
            out.append(Judgement(mint, bool(item.get("trade")), probability, "model"))
    if not out:
        raise ValueError("model returned no usable judgements")
    return sorted(out, key=lambda j: (j.trade, j.probability), reverse=True)
