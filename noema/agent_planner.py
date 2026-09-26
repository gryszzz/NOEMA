from __future__ import annotations

from dataclasses import dataclass

from .opportunity_radar import RadarRow


@dataclass(frozen=True)
class AgentGoalSelection:
    goal: str
    reason: str


def choose_goal(
    *,
    radar: list[RadarRow],
    kalshi_healthy: bool,
    wallet_healthy: bool,
    economic_initialized: bool,
) -> AgentGoalSelection:
    if not kalshi_healthy:
        return AgentGoalSelection(
            "restore_market_perception",
            "Kalshi/account perception is degraded",
        )
    if not wallet_healthy:
        return AgentGoalSelection(
            "restore_wallet_perception",
            "dedicated EVM wallet perception is degraded",
        )
    if not economic_initialized:
        return AgentGoalSelection(
            "initialize_economic_memory",
            "Economic OS has no initialized snapshot",
        )

    scored = [row for row in radar if row.attention_score is not None]
    if scored and scored[0].attention_score is not None and scored[0].attention_score >= 0.70:
        return AgentGoalSelection(
            "investigate_high_attention_market",
            f"research radar attention={scored[0].attention_score:.3f}",
        )

    if radar:
        return AgentGoalSelection(
            "calibrate_and_collect",
            f"{len(radar)} recent market states available; no high-attention condition",
        )

    return AgentGoalSelection(
        "collect_world_state",
        "No forecast-ledger radar state is available yet",
    )
