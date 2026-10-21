"""Bayesian shrinkage of agent influence, and automatic benching.

Weight starts at zero. It rises only as resolved forecasts show calibration
better than the configured ceiling. After N resolutions, an agent that is still
outside that ceiling is benched: it keeps forecasting in shadow at weight zero.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .accuracy_config import AccuracyConfig
from .accuracy_scoring import score_agents

_BENCHMARK_MARKERS = ("market-baseline", "market-midpoint")


@dataclass(frozen=True)
class AgentInfluence:
    agent_id: str
    weight: float
    benched: bool
    role: str
    reason: str
    sample_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "weight": round(self.weight, 4),
            "benched": self.benched,
            "role": self.role,
            "reason": self.reason,
            "sample_size": self.sample_size,
        }


def _role(agent_id: str) -> str:
    if any(marker in agent_id for marker in _BENCHMARK_MARKERS):
        return "benchmark"
    if agent_id.endswith(":ohlcv_shadow"):
        return "research"
    if agent_id.startswith("trench_desk:"):
        return "execution"
    if agent_id == "chief":
        return "aggregator"
    return "forecaster"


def influence_for(
    agent_id: str, metrics: dict[str, Any] | None, config: AccuracyConfig
) -> AgentInfluence:
    role = _role(agent_id)
    sample_size = 0 if metrics is None else int(metrics.get("sample_size") or 0)
    if role != "forecaster":
        return AgentInfluence(
            agent_id, 0.0, False, role, f"{role} does not receive influence", sample_size
        )
    if sample_size <= 0 or metrics is None or metrics.get("brier") is None:
        return AgentInfluence(
            agent_id,
            0.0,
            False,
            role,
            "no resolved forecasts; weight stays at zero",
            0,
        )
    brier = float(metrics["brier"])
    ece = float(metrics["calibration_error"] or 0.0)
    ece_skill = (
        0.0
        if config.max_calibration_error <= 0
        else max(
            0.0,
            1.0 - ece / config.max_calibration_error,
        )
    )
    brier_skill = 0.0 if config.max_brier <= 0 else max(0.0, 1.0 - brier / config.max_brier)
    skill = min(ece_skill, brier_skill)
    shrink = sample_size / (sample_size + config.prior_strength)
    weight = min(config.max_weight, shrink * skill)
    benched = sample_size >= config.min_resolved and (
        ece > config.max_calibration_error or brier > config.max_brier
    )
    if benched:
        return AgentInfluence(
            agent_id,
            0.0,
            True,
            role,
            f"benched after {sample_size} resolutions (calibration {ece:.3f}, brier {brier:.3f})",
            sample_size,
        )
    return AgentInfluence(
        agent_id,
        weight,
        False,
        role,
        f"shrink {shrink:.3f} × skill {skill:.3f} from {sample_size} resolutions",
        sample_size,
    )


def influences_for(
    conn: sqlite3.Connection, config: AccuracyConfig | None = None
) -> dict[str, AgentInfluence]:
    config = config or AccuracyConfig.from_env()
    try:
        known = [row[0] for row in conn.execute("SELECT DISTINCT agent_id FROM accuracy_forecasts")]
    except sqlite3.OperationalError:
        return {}
    scored = {item["agent_id"]: item for item in score_agents(conn, config=config)}
    return {agent_id: influence_for(agent_id, scored.get(agent_id), config) for agent_id in known}


def apply_influence(
    picks: list[Any],
    log: list[dict[str, Any]],
    influences: dict[str, AgentInfluence],
    *,
    min_probability: float,
    base_rate: float,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Drop or shrink chief approvals whose source agent has no proven weight."""
    source_agent = {"model": "tev_judge", "rules": "trench_rules"}
    by_mint = {entry["mint"]: dict(entry) for entry in log}
    kept: list[Any] = []
    order: list[str] = []
    for pick in picks:
        agent_id = source_agent.get(getattr(pick, "source", ""), getattr(pick, "source", "unknown"))
        info = influences.get(agent_id) or influence_for(agent_id, None, AccuracyConfig())
        shrunk = info.weight * float(pick.probability) + (1.0 - info.weight) * base_rate
        entry = by_mint.get(pick.mint, {"mint": pick.mint, "approved": True})
        entry["agent_id"] = agent_id
        entry["weight"] = round(info.weight, 4)
        entry["benched"] = info.benched
        entry["shrunk_probability"] = round(shrunk, 4)
        if info.benched or info.weight <= 0:
            entry["approved"] = False
            entry["shadow"] = True
            entry["reason"] = info.reason
        elif shrunk < min_probability:
            entry["approved"] = False
            entry["shadow"] = True
            entry["reason"] = (
                f"shrunk p={shrunk:.2f} below {min_probability:.2f} at weight {info.weight:.2f}"
            )
        else:
            entry["approved"] = True
            entry["shadow"] = False
            entry["reason"] = f"weight {info.weight:.2f} shrunk p={shrunk:.2f}"
            kept.append(pick)
        by_mint[pick.mint] = entry
        order.append(pick.mint)
    for entry in log:
        order.append(entry["mint"])
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for mint in order:
        if mint in seen or mint not in by_mint:
            continue
        seen.add(mint)
        merged.append(by_mint[mint])
    return kept, merged
