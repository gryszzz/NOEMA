from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class RadarRow:
    venue: str
    market_id: str
    title: str
    probability_yes: float
    market_probability: float
    yes_ask: float | None
    raw_edge: float
    estimated_cost: float
    uncertainty_penalty: float
    robust_edge: float
    spread: float | None
    liquidity_usd: float | None
    captured_at: str
    freshness_seconds: float
    uncertainty_width: float
    attention_score: float | None
    decision: str
    reason: str
    evidence_ids: tuple[str, ...]
    model_version: str = "unknown"
    attention_score_suppression_reason: str | None = None
    missing_inputs: tuple[str, ...] = ()


_POLITICAL_TERMS = {
    "election",
    "president",
    "presidential",
    "senate",
    "senator",
    "congress",
    "congressional",
    "governor",
    "gubernatorial",
    "democrat",
    "republican",
    "ballot",
    "primary election",
}


def _political_like(title: str) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in _POLITICAL_TERMS)


def _attention_score_suppression_reason(model_version: str, title: str) -> str | None:
    """Explain policy suppression instead of making a missing score look accidental."""
    if model_version in {"market-baseline-v1", "market-midpoint-v1"}:
        return "benchmark_forecast_has_no_independent_edge"
    if model_version == "series-frequency-v1":
        return "research_candidate_not_promoted"
    if _political_like(title):
        return "political_market_policy"
    if model_version == "unknown":
        return "forecast_model_version_missing"
    return None


def _attention_score(
    *,
    robust_edge: float,
    spread: float | None,
    liquidity_usd: float | None,
    freshness_seconds: float,
    uncertainty_width: float,
) -> float:
    edge = min(max(robust_edge / 0.10, 0.0), 1.0)
    spread_quality = 0.0 if spread is None else max(0.0, 1 - min(spread / 0.10, 1.0))
    liquidity = 0.0 if not liquidity_usd else min(liquidity_usd / 10_000.0, 1.0)
    freshness = max(0.0, 1 - min(freshness_seconds / 300.0, 1.0))
    certainty = max(0.0, 1 - min(uncertainty_width / 0.30, 1.0))
    return (
        0.30 * edge
        + 0.20 * spread_quality
        + 0.15 * liquidity
        + 0.20 * freshness
        + 0.15 * certainty
    )


def build_radar(
    path: str = "data/noema.db",
    *,
    limit: int = 50,
    now: datetime | None = None,
) -> list[RadarRow]:
    if not Path(path).exists():
        return []
    now = now or datetime.now(UTC)
    conn = sqlite3.connect(path)

    try:
        rows = conn.execute(
            """
            SELECT venue, market_id, snapshot_json, forecast_json,
                   opportunity_json, action_json, created_at
            FROM forecast_ledger
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []

    seen: set[tuple[str, str]] = set()
    radar: list[RadarRow] = []
    for venue, market_id, snapshot_json, forecast_json, opportunity_json, action_json, created_at in rows:
        key = (venue, market_id)
        if key in seen:
            continue
        seen.add(key)

        snapshot = json.loads(snapshot_json)
        forecast = json.loads(forecast_json)
        opportunity = json.loads(opportunity_json)
        action = json.loads(action_json)

        bid = snapshot.get("yes_bid")
        ask = snapshot.get("yes_ask")
        spread = None
        if bid is not None and ask is not None:
            spread = float(ask) - float(bid)

        captured_raw = snapshot.get("captured_at") or created_at
        captured = datetime.fromisoformat(str(captured_raw))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        freshness = max(0.0, (now - captured.astimezone(UTC)).total_seconds())

        missing_inputs: list[str] = []
        for name in ("probability_yes", "lower_bound", "upper_bound", "model_version"):
            if name not in forecast or forecast.get(name) is None:
                missing_inputs.append(f"forecast.{name}")
        for name in ("raw_edge", "estimated_cost", "uncertainty_penalty", "robust_edge"):
            if name not in opportunity or opportunity.get(name) is None:
                missing_inputs.append(f"opportunity.{name}")
        if ask is None:
            missing_inputs.append("snapshot.yes_ask")
        if bid is None:
            missing_inputs.append("snapshot.yes_bid")

        lower = float(forecast.get("lower_bound", 0.0))
        upper = float(forecast.get("upper_bound", 1.0))
        robust_edge = float(opportunity.get("robust_edge", 0.0))

        title = str(snapshot.get("title") or market_id)
        model_version = str(forecast.get("model_version") or "unknown")
        suppression_reason = _attention_score_suppression_reason(model_version, title)
        score = None
        if suppression_reason is None:
            score = _attention_score(
                robust_edge=robust_edge,
                spread=spread,
                liquidity_usd=snapshot.get("liquidity_usd"),
                freshness_seconds=freshness,
                uncertainty_width=max(0.0, upper - lower),
            )

        radar.append(
            RadarRow(
                venue=str(venue),
                market_id=str(market_id),
                title=title,
                probability_yes=float(forecast.get("probability_yes", 0.5)),
                market_probability=float(opportunity.get("market_probability", 0.5)),
                yes_ask=(None if ask is None else float(ask)),
                raw_edge=float(opportunity.get("raw_edge", 0.0)),
                estimated_cost=float(opportunity.get("estimated_cost", 0.0)),
                uncertainty_penalty=float(
                    opportunity.get("uncertainty_penalty", 0.0)
                ),
                robust_edge=robust_edge,
                spread=spread,
                liquidity_usd=(
                    None
                    if snapshot.get("liquidity_usd") is None
                    else float(snapshot["liquidity_usd"])
                ),
                captured_at=captured.astimezone(UTC).isoformat(),
                freshness_seconds=freshness,
                uncertainty_width=max(0.0, upper - lower),
                attention_score=score,
                decision=str(action.get("decision") or "unknown"),
                reason=str(action.get("reason") or ""),
                evidence_ids=tuple(forecast.get("evidence_ids") or ()),
                model_version=model_version,
                attention_score_suppression_reason=suppression_reason,
                missing_inputs=tuple(missing_inputs),
            )
        )

    radar.sort(
        key=lambda row: (
            row.attention_score is not None,
            row.attention_score if row.attention_score is not None else -1.0,
        ),
        reverse=True,
    )
    return radar
