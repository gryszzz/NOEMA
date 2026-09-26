from __future__ import annotations

from dataclasses import dataclass

from .cognition_models import CognitionResult
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


def refine_goal_with_cognition(
    goal: AgentGoalSelection,
    cognition: CognitionResult,
) -> AgentGoalSelection:
    if cognition.status != "completed" or cognition.packet is None:
        return goal

    if goal.goal.startswith("restore_") or goal.goal == "initialize_economic_memory":
        return goal

    if cognition.packet.recommended_mode == "investigate":
        return AgentGoalSelection(
            "model_guided_investigation",
            (
                f"Foundry cognition confidence={cognition.packet.confidence:.3f}; "
                f"{cognition.packet.attention_reason}"
            ),
        )
    if cognition.packet.recommended_mode == "collect_more":
        return AgentGoalSelection(
            "collect_requested_research",
            (
                f"Foundry requested additional research; "
                f"confidence={cognition.packet.confidence:.3f}"
            ),
        )
    return goal
